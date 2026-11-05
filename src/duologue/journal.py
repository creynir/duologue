"""What both append paths share: their outcomes and the intent recorded before every write.

An append records what it is about to write (:class:`Intent`) before it writes a byte, through a
:class:`Journal` the caller provides. If duologue stops partway, the next run finds the intent and
works out from the file whether the write is missing, cut off or complete, instead of guessing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Wait(Exception):
    """The other app is busy with the conversation; a later pass tries again."""


class Blocked(Exception):
    """Something needs a person: the pair stops syncing until someone releases it."""


@dataclass(frozen=True)
class Intent:
    """An append about to happen: ``data`` goes at byte ``base`` of ``path``."""

    side: str  # "codex" or "claude"
    path: Path
    base: int  # the file's size before the write
    data: bytes
    ids: tuple[str, ...]  # Codex turn ids or Claude record uuids, as written

    @property
    def end(self) -> int:
        return self.base + len(self.data)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class Journal(Protocol):
    def begin(self, intent: Intent) -> None:
        """Make ``intent`` durable. Called before the write; the write waits for it."""


class MemoryJournal:
    """A journal that only remembers the last intent (for callers that recover nothing)."""

    def __init__(self) -> None:
        self.intent: Intent | None = None

    def begin(self, intent: Intent) -> None:
        self.intent = intent


def tail_state(path: Path, intent: Intent) -> str:
    """Where an interrupted write stands in ``path``: ``"absent"`` (nothing of it is there),
    ``"partial"`` (a cut-off start of it), ``"complete"`` (all of it) or ``"changed"`` (the file
    no longer matches what was written, or something else was written after it)."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "changed"
    if size == intent.base:
        return "absent"
    if size < intent.base:
        return "changed"
    with open(path, "rb") as fh:
        fh.seek(intent.base)
        written = fh.read(len(intent.data) + 1)
    if written == intent.data:
        return "complete"
    if len(written) < len(intent.data) and intent.data.startswith(written):
        return "partial"
    if written.startswith(intent.data):
        return "complete"  # the app appended after it: still all there
    return "changed"
