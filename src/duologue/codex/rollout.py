"""Codex rollout files: finding them, reading them line by line, and where their turns end.

A rollout is ``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl`` (or
under ``archived_sessions/``). Its first line is ``session_meta``; every later line carries a
``timestamp``, a ``type`` and a ``payload``, and in paginated threads an ``ordinal``. Idle rollouts
are eventually compressed to ``.jsonl.zst``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import tomllib
import uuid
from pathlib import Path

from duologue.ids import require_id
from duologue.jsonl import Line

SESSIONS = "sessions"
ARCHIVED = "archived_sessions"
STATE_DB = "state_5.sqlite"
TURN_ENDS = ("task_complete", "turn_aborted")


def find_rollout(codex_home: Path, thread_id: str, sqlite: Path | None = None) -> Path | None:
    """The thread's rollout: the path Codex's state database (in ``sqlite``, default the home)
    records, when the file is there, else the newest file named for the thread under
    ``sessions/`` or ``archived_sessions/``."""
    require_id(thread_id, "thread id")
    home = Path(codex_home)
    recorded = _recorded_path(Path(sqlite or home), thread_id)
    if recorded is not None and recorded.exists():
        return recorded
    found = []
    for folder in (SESSIONS, ARCHIVED):
        base = home / folder
        if base.is_dir():
            found += base.rglob(f"rollout-*-{thread_id}.jsonl")
            found += base.rglob(f"rollout-*-{thread_id}.jsonl.zst")
    # A readable copy wins over a compressed one, then the newest; Codex may move one meanwhile.
    stamped = [(not is_compressed(p), _mtime(p), p) for p in found]
    stamped = [(plain, t, p) for plain, t, p in stamped if t is not None]
    return max(stamped)[2] if stamped else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _recorded_path(sqlite: Path, thread_id: str) -> Path | None:
    db = sqlite / STATE_DB
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute(
                "select rollout_path from threads where id = ?", (thread_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return Path(row[0]) if row and row[0] else None


def is_compressed(path: Path) -> bool:
    return path.name.endswith(".zst")


def session_meta(path: Path) -> dict:
    """The rollout's ``session_meta`` payload (empty when the first line is not one)."""
    with open(path, "rb") as fh:
        first = fh.readline()
    try:
        record = json.loads(first)
    except ValueError:
        return {}
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return {}
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def history_mode(path: Path) -> str | None:
    """``"paginated"`` or ``"legacy"``, as the rollout's first line declares it."""
    return session_meta(path).get("history_mode")


def payload(record: dict) -> dict:
    p = record.get("payload")
    return p if isinstance(p, dict) else {}


def turn_ends(lines: list[Line]) -> list[tuple[str, int]]:
    """(turn id, offset just after its last line) for every turn that finished in ``lines``."""
    out = []
    for ln in lines:
        p = payload(ln.record)
        if ln.record.get("type") == "event_msg" and p.get("type") in TURN_ENDS and p.get("turn_id"):
            out.append((p["turn_id"], ln.end))
    return out


def open_turn(lines: list[Line]) -> bool:
    """Whether the last turn in ``lines`` started and has not finished: Codex is working on it."""
    state = False
    for ln in lines:
        if ln.record.get("type") != "event_msg":
            continue
        kind = payload(ln.record).get("type")
        if kind == "task_started":
            state = True
        elif kind in TURN_ENDS:
            state = False
    return state


def model_provider(codex_home: Path) -> str | None:
    """The model provider new threads use, as Codex's config sets it: the active profile's
    ``model_provider``, else the top-level one, else Codex's default, openai. None when the
    config cannot be read: Codex lists only threads of its active provider, so guessing would
    hide the thread."""
    path = Path(codex_home) / "config.toml"
    try:
        with open(path, "rb") as fh:
            config = tomllib.load(fh)
    except FileNotFoundError:
        return "openai"
    except (OSError, tomllib.TOMLDecodeError):
        return None
    profile = config.get("profile")
    profiles = config.get("profiles")
    if isinstance(profile, str) and isinstance(profiles, dict):
        chosen = profiles.get(profile)
        value = chosen.get("model_provider") if isinstance(chosen, dict) else None
        if isinstance(value, str) and value:
            return value
    value = config.get("model_provider")
    return value if isinstance(value, str) and value else "openai"


def sqlite_home(codex_home: Path, env: dict[str, str]) -> Path:
    """Where Codex keeps its databases: ``sqlite_home`` in its config, else
    ``$CODEX_SQLITE_HOME``, else its home (codex-rs config_toml.rs)."""
    try:
        with open(Path(codex_home) / "config.toml", "rb") as fh:
            value = tomllib.load(fh).get("sqlite_home")
    except (OSError, tomllib.TOMLDecodeError):
        value = None
    if isinstance(value, str) and value:
        return Path(value).expanduser()
    if env.get("CODEX_SQLITE_HOME"):
        return Path(env["CODEX_SQLITE_HOME"]).expanduser()
    return Path(codex_home)


def is_archived(codex_home: Path, path: Path) -> bool:
    return Path(codex_home) / ARCHIVED in path.parents


def uuid7(ms: int | None = None) -> str:
    """A UUIDv7 (time-ordered), the kind of id Codex gives its threads."""
    ms = int(time.time() * 1000) if ms is None else ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def new_rollout_path(codex_home: Path, thread_id: str, created_ms: int) -> Path:
    """Where Codex keeps a thread created at ``created_ms``: dated by local time, as Codex does."""
    local = time.localtime(created_ms / 1000)
    folder = Path(codex_home) / SESSIONS / time.strftime("%Y/%m/%d", local)
    return folder / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', local)}-{thread_id}.jsonl"
