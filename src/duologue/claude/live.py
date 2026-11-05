"""Whether a Claude Code process has a session open.

A running Claude Code keeps the session in memory. Records appended to its transcript meanwhile
are lost: its next message links to its own last record and leaves them on a side branch. So
duologue writes to a transcript only while no process has the session.

Claude Code registers every running session in ``<CLAUDE_CONFIG_DIR>/sessions/<pid>.json``
(``sessionId``, ``pid``, ``status``). A process that resumed the session by id also names it on
its command line, which covers one that has not registered yet.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REGISTRY = "sessions"
RESUME_FLAGS = ("--resume", "-r", "--session-id")


def live_pid(claude_home: Path, session_id: str) -> int | None:
    """The pid of a running Claude Code process that has the session, or None."""
    return _registered(Path(claude_home) / REGISTRY, session_id) or _resumed(session_id)


def _registered(registry: Path, session_id: str) -> int | None:
    if not registry.is_dir():
        return None
    for path in registry.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
            continue
        pid = entry.get("pid")
        if isinstance(pid, int) and alive(pid):
            return pid
    return None


def _resumed(session_id: str) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        words = args.split()
        if "claude" not in args or "duologue" in args:
            continue
        named = any(w == session_id or w.endswith("=" + session_id) for w in words)
        flagged = any(w.split("=", 1)[0] in RESUME_FLAGS for w in words)
        if named and flagged:
            return int(pid_text)
    return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, under another user
    return True
