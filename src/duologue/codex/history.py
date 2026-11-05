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
    """One thread's checkpoint and turns. ``next_offset`` is None when Codex has not indexed the
    thread at all."""

    next_offset: int | None = None
    next_ordinal: int | None = None
    turns: dict[str, str] = field(default_factory=dict)  # turn id -> status, in rollout order


def read_index(codex_home: Path, thread_id: str) -> HistoryIndex:
    db = Path(codex_home) / HISTORY_DB
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
        return HistoryIndex(turns=dict(turns))
    return HistoryIndex(state[0], state[1], dict(turns))
