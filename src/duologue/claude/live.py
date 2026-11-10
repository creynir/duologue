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
# ps cuts command lines to the terminal's width, 80 columns when piped; -ww lifts the limit.
UNKNOWN = -1  # the process list could not be read: the session may be open


def live_pid(claude_home: Path, session_id: str) -> int | None:
    """The pid of a running Claude Code process that has the session, :data:`UNKNOWN` when the
    process list cannot be read, or None."""
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
        if isinstance(pid, int) and alive(pid) and _is_claude(pid):
            return pid
    return None


def _is_claude(pid: int) -> bool:
    """Whether ``pid`` is still a Claude Code process (a stale entry's pid can be reused)."""
    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: assume the session is open
    return "claude" in out.lower()


def _resumed(session_id: str) -> int | None:
    try:
        r = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if r.returncode != 0:
        return UNKNOWN
    out = r.stdout
    for line in out.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        words = args.split()
        if _is_claude_code(words) and _resumes(words, session_id):
            return int(pid_text)
    return None


def _is_claude_code(words: list[str]) -> bool:
    """Whether a command line runs Claude Code itself: its ``claude`` program, or Node or Bun
    running its ``cli.js``. Not a shell or script that merely mentions such a command."""
    if not words:
        return False
    program = os.path.basename(words[0])
    if program == "claude":
        return True
    return program in ("node", "bun") and any(
        "claude-code" in w and os.path.basename(w) == "cli.js" for w in words[1:3]
    )


def _resumes(words: list[str], session_id: str) -> bool:
    """Whether a command line resumes ``session_id``: a resume flag with the id as its value."""
    for n, w in enumerate(words):
        flag, eq, value = w.partition("=")
        if flag not in RESUME_FLAGS:
            continue
        if (value if eq else (words[n + 1] if n + 1 < len(words) else "")) == session_id:
            return True
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, under another user
    return True
