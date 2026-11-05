"""Long tool outputs: the start stays in the conversation, the whole of it goes to a file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from duologue.jsonl import clean

# What a model reads of one tool output, and what an app shows.
MODEL_LIMIT = 4000
UI_LIMIT = 20000


class OutputStore:
    """Caps tool outputs and keeps their full text in ``directory``, one file per output.

    Both agents must be able to read ``directory``: the pointer in the capped text is how the other
    side's model reads the rest on demand.
    """

    def __init__(
        self, directory: str | os.PathLike, model_limit: int = MODEL_LIMIT, ui_limit: int = UI_LIMIT
    ):
        self.directory = Path(directory)
        self.model_limit = model_limit
        self.ui_limit = ui_limit

    def keep(self, ref: str, text: object, limit: int | None = None) -> str:
        """The first ``limit`` characters of ``text`` (default: the model limit), preceded by where
        the full text is when it was longer. ``ref`` names the file; the same ref and text always
        give the same file."""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        limit = self.model_limit if limit is None else limit
        if len(text) <= limit:
            return text
        path = self._write(ref, text)
        # The pointer goes first: a later reader that cuts the text again keeps it.
        return (
            f"[{len(text)} characters; the full output is in {path}; the start of it:]\n"
            + text[:limit]
        )

    def _write(self, ref: str, text: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        ref = str(ref)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", ref)
        if name != ref or len(name) > 80:
            # A changed or shortened name could match another ref's: the digest keeps them apart.
            name = name[:64] + "-" + hashlib.sha256(ref.encode()).hexdigest()[:12]
        path = self.directory / f"{name}.txt"
        text = clean(text)
        if _ours(path) and _read(path) == text:
            return path  # already kept: every pass would otherwise write it again
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return path


def _ours(path: Path) -> bool:
    """A regular, owner-only file of this user, not a link that points elsewhere."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.getuid()
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # damaged or unreadable: written again
