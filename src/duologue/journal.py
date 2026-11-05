"""What both append paths share: their outcomes and the intent recorded before every write.

An append records what it is about to write (:class:`Intent`) before it writes a byte, through a
:class:`Journal` the caller provides. If duologue stops partway, the next run finds the intent and
works out from the file whether the write is missing, cut off or complete, instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Wait(Exception):
    """The other app is busy with the conversation; a later pass tries again. When it is
    raised after a write's intent was recorded (``Journal.begin``), that intent is left to settle
    with ``recover`` first."""


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
    before: str = ""  # sha256 of the file's first ``base`` bytes, to notice a rewrite

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
    """Where an interrupted write stands in ``path``:

    - ``"absent"``: nothing of it is there (for a new file: the file is not there; with a
      ``before`` digest that still matches, another writer's line may follow ``base``);
    - ``"partial"``: a cut-off start of it ends the file;
    - ``"complete"``: all of it, and the file ends with it;
    - ``"followed"``: all of it, and an app wrote more after it;
    - ``"changed"``: the file no longer matches what was written.

    Only ``"partial"`` and ``"complete"`` leave nothing but duologue's bytes after ``base``.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "absent" if intent.base == 0 else "changed"
    if size < intent.base:
        return "changed"
    if intent.before and prefix_hash(path, intent.base) != intent.before:
        return "changed"
    if size == intent.base:
        return "absent"
    with open(path, "rb") as fh:
        fh.seek(intent.base)
        written = fh.read(len(intent.data) + 1)
    if written == intent.data:
        return "complete"
    if len(written) < len(intent.data) and intent.data.startswith(written):
        return "partial"
    if written.startswith(intent.data):
        return "followed"
    if intent.before and _displaced(path, intent):
        # The file before base is as it was, and a whole line of another writer's follows it:
        # our write never landed there, or was taken back before the app wrote its own.
        return "absent"
    return "changed"


def _displaced(path: Path, intent: Intent) -> bool:
    """Whether the line at ``base`` is a whole JSON line that is not ours."""
    ours = intent.data.split(b"\n", 1)[0] + b"\n"
    with open(path, "rb") as fh:
        fh.seek(intent.base)
        line = fh.readline()
    if not line.endswith(b"\n") or line == ours:
        return False
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False  # our cut-off start with another writer's bytes glued on


def prefix_hash(path: Path, length: int) -> str:
    """The sha256 of the first ``length`` bytes of ``path``."""
    digest, left = hashlib.sha256(), length
    with open(path, "rb") as fh:
        while left > 0:
            chunk = fh.read(min(left, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            left -= len(chunk)
    return digest.hexdigest()
