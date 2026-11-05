"""Codex's history index (``thread_history_1.sqlite``), read-only.

A paginated thread renders only from this index. Codex builds it from the rollout and keeps a
checkpoint per thread: the byte offset and ordinal where the next rollout line starts. duologue
never writes the index; it reads it to know where Codex stands, and asks Codex to catch up with
``thread/resume``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

HISTORY_DB = "thread_history_1.sqlite"


@dataclass(frozen=True)
class HistoryIndex:
    """One thread's checkpoint and turns. ``found`` is False when Codex's index is not there at
    all (a new home, or a Codex that keeps it elsewhere); ``next_offset`` is None when Codex has
    not indexed this thread."""

    next_offset: int | None = None
    next_ordinal: int | None = None
    turns: dict[str, str] = field(default_factory=dict)  # turn id -> status, in rollout order
    found: bool = False

    def holds(self, turns: tuple[str, ...], end: int) -> bool:
        """Whether Codex indexed ``turns`` whole, through byte ``end``."""
        return (
            (self.next_offset or 0) >= end
            and all(t in self.turns for t in turns)
            and all(self.turns[t] != "inProgress" for t in turns)
        )


def read_index(sqlite_home: Path, thread_id: str) -> HistoryIndex:
    """The thread's index, from the folder Codex keeps its databases in."""
    db = Path(sqlite_home) / HISTORY_DB
    if not db.exists():
        return HistoryIndex()
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        state = con.execute(
            "select next_rollout_byte_offset, next_rollout_ordinal "
            "from thread_history_projection_state where thread_id = ?",
            (thread_id,),
        ).fetchone()
        turns = con.execute(
            "select turn_id, status from thread_turns where thread_id = ? order by rollout_ordinal",
            (thread_id,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):  # a home where Codex has not built the index yet
            return HistoryIndex()
        raise
    finally:
        con.close()
    if state is None:
        return HistoryIndex(turns=dict(turns), found=True)
    return HistoryIndex(state[0], state[1], dict(turns), found=True)
