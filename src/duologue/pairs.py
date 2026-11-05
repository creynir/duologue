"""Linked pairs and where each one's sync stands, kept in ``<home>/pairs.json``.

A pair's cursors say how far each side has been synced: everything before ``end`` (a byte offset)
is accounted for. Each cursor keeps the sha256 of the file up to ``end``; when that changes, the
file was rewritten under duologue, and the pair stops instead of guessing (steal-list item 1).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_VERSION = 1
LOG_LIMIT = 5 * 1024 * 1024  # bytes; the log then starts again, keeping one previous file


class Busy(Exception):
    """Another duologue command holds the state."""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Cursor:
    """``end``: the byte offset everything before which is synced. ``digest``: the sha256 of the
    file's first ``end`` bytes. ``anchor``: the last Claude record uuid or Codex turn id before
    ``end``, to find the spot again after Codex rewrites a thread."""

    end: int = 0
    digest: str = ""
    anchor: str | None = None


@dataclass
class Pending:
    """An append that started and is not confirmed yet. Its bytes are in the journal file."""

    side: str  # "codex" or "claude": the side written to
    path: str
    base: int
    length: int
    sha256: str
    ids: list[str]
    claude: dict  # the cursors to set once the write is confirmed (end and anchor)
    codex: dict


@dataclass
class Pair:
    name: str
    claude: str  # Claude Code session id
    codex: str  # Codex thread id
    created: str = field(default_factory=now_iso)
    claude_cursor: Cursor = field(default_factory=Cursor)
    codex_cursor: Cursor = field(default_factory=Cursor)
    hold: str | None = None  # why the user paused it
    blocked: str | None = None  # what needs a person
    conflict: bool = False  # the user wrote on both sides since the last sync
    pending: Pending | None = None
    status: str = ""  # what the last pass did or waits for
    last_sync: str | None = None
    copied: dict = field(default_factory=lambda: {"to_codex": 0, "to_claude": 0})  # turns
    seen: list = field(default_factory=list)  # file sizes and mtimes when last up to date

    @property
    def key(self) -> str:
        """What the ids duologue writes for this pair derive from."""
        return f"{self.claude}/{self.codex}"

    @property
    def state(self) -> str:
        if self.hold:
            return "held"
        if self.blocked:
            return "blocked"
        if self.conflict:
            return "conflict"
        return "linked"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Pair:
        d = dict(d)
        d["claude_cursor"] = Cursor(**d.get("claude_cursor") or {})
        d["codex_cursor"] = Cursor(**d.get("codex_cursor") or {})
        d["pending"] = Pending(**d["pending"]) if d.get("pending") else None
        return cls(**d)


class Store:
    """The pairs file. Writes are atomic: a crash leaves the old file or the new one."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, Pair]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"{self.path}: state version {data.get('version')} is not supported")
        return {p["name"]: Pair.from_dict(p) for p in data.get("pairs", [])}

    def save(self, pairs: dict[str, Pair]) -> None:
        data = {"version": STATE_VERSION, "pairs": [p.to_dict() for p in pairs.values()]}
        write_atomic(self.path, (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode())


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@contextlib.contextmanager
def exclusive(lock_file: Path, wait: float = 60.0) -> Iterator[None]:
    """One duologue command at a time changes the state: the timer's pass and a command typed
    meanwhile take turns."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("another duologue command is running; try again shortly") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


class ActivityLog:
    """What duologue did, one JSON line per event, for ``duologue log``."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, pair: str | None, event: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if self.path.stat().st_size > LOG_LIMIT:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        line = {"time": now_iso(), "pair": pair, "event": event, "text": text}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20, pair: str | None = None) -> list[dict]:
        out = []
        for path in (self.path.with_name(self.path.name + ".1"), self.path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            for raw in lines:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if pair is None or entry.get("pair") == pair:
                    out.append(entry)
        return out[-n:] if n > 0 else out
