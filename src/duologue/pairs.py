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
import stat
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
    before: str = ""  # sha256 of the file's first ``base`` bytes


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
    checked: dict = field(default_factory=dict)  # side -> file stamp its cursor last matched
    # [Codex cursor end, Codex file size] when a pass last found nothing in Codex to copy
    codex_quiet: list | None = None
    # A note to put before the next Claude turns copied to Codex (turns left out before them)
    note_to_codex: str | None = None

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
        # Fields a later duologue added are left out: the pair still syncs with this one.
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
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
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            version = data.get("version") if isinstance(data, dict) else None
            raise ValueError(f"{self.path}: state version {version} is not supported")
        pairs = data.get("pairs")
        if not isinstance(pairs, list) or not all(isinstance(p, dict) for p in pairs):
            raise ValueError(f"{self.path}: the pairs are not a list of objects")
        try:
            return {p["name"]: Pair.from_dict(p) for p in pairs}
        except (KeyError, TypeError) as e:
            raise ValueError(f"{self.path}: a pair is not readable ({e})") from e

    def save(self, pairs: dict[str, Pair]) -> None:
        data = {"version": STATE_VERSION, "pairs": [p.to_dict() for p in pairs.values()]}
        write_atomic(self.path, (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode())


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def open_private(path: Path) -> int:
    """Open ``path`` for appending as an owner-only file of this user. A symbolic or hard link
    planted in its place, or anything but such a file, is replaced, never written through (so
    no other file is changed)."""
    # O_NONBLOCK: a named pipe planted there must not hang the open before it can be refused.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    for _ in range(2):
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            if stat.S_ISREG(os.lstat(path).st_mode):
                raise
            os.unlink(path)  # a link, or a pipe nobody reads
            continue
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid():
            os.fchmod(fd, 0o600)  # also a file created with a looser mode
            return fd
        os.close(fd)
        os.unlink(path)
    raise OSError(f"{path}: could not be opened as a private file")


class ActivityLog:
    """What duologue did, one JSON line per event, for ``duologue log``."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, pair: str | None, event: str, text: str) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if self.path.stat().st_size > LOG_LIMIT:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        line = {"time": now_iso(), "pair": pair, "event": event, "text": text}
        fd = open_private(self.path)
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


# ------------------------------------------------------------------------- pairing by itself
class AutoStateError(ValueError):
    """``auto.json`` cannot be read. It lists what the user unlinked, so nothing is paired by
    itself until it is fixed or removed."""


@dataclass
class AutoState:
    """``<home>/auto.json``: the ids not to pair by itself (the user's decisions), and what the
    last passes found (bookkeeping)."""

    path: Path
    ignored: set[str] = field(default_factory=set)
    passed: dict[str, list] = field(default_factory=dict)  # path -> [size, mtime_ns, why]
    failed: dict[str, list] = field(default_factory=dict)  # path -> [when, why]
    waiting: dict[str, str] = field(default_factory=dict)  # id -> why, in the last pass
    settings: dict = field(default_factory=dict)  # the ones the reasons in ``passed`` rest on
    changed: bool = False

    @classmethod
    def load(cls, path: Path) -> AutoState:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return cls(path)
        if not stat.S_ISREG(st.st_mode):
            raise AutoStateError(f"{path} is not a file")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AutoStateError(f"{path} cannot be read ({e})") from e
        if not isinstance(data, dict) or not isinstance(data.get("ignored", []), list):
            raise AutoStateError(f"{path} does not hold what duologue writes there")

        def mapping(key: str) -> dict:
            value = data.get(key)
            return dict(value) if isinstance(value, dict) else {}

        return cls(
            path,
            {x for x in data.get("ignored", []) if isinstance(x, str)},
            mapping("passed"),
            mapping("failed"),
            mapping("waiting"),
            mapping("settings"),
        )

    def pass_over(self, path: str, stamp: tuple[int, int], why: str) -> None:
        """Remember that the conversation in ``path`` was left out as it stands, and why."""
        self.passed[path] = [*stamp, why]
        self.failed.pop(path, None)
        self.changed = True

    def fail(self, path: str, why: str, now: float) -> bool:
        """Remember that a try at the conversation in ``path`` failed; whether that is news (not
        the reason logged last time)."""
        old = self.failed.get(path)
        self.failed[path] = [now, why]
        self.changed = True
        return not (isinstance(old, list) and old[1:] == [why])

    def save(self) -> None:
        data = {
            "ignored": sorted(self.ignored),
            "passed": self.passed,
            "failed": self.failed,
            "waiting": self.waiting,
            "settings": self.settings,
        }
        write_atomic(self.path, (json.dumps(data, indent=1) + "\n").encode())

    def ignore(self, *ids: str) -> None:
        self.ignored.update(ids)
        self.save()

    def unignore(self, *ids: str) -> None:
        if self.ignored & set(ids):
            self.ignored -= set(ids)
            self.save()


def auto_state_file(config) -> Path:
    """``auto.json`` in duologue's folder (``config`` is a :class:`~duologue.config.Config`)."""
    return config.home / "auto.json"
