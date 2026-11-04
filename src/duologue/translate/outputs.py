"""Long tool outputs: the start stays in the conversation, the whole of it goes to a file."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

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
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(ref))
        if len(name) > 80:
            name = name[:64] + "-" + hashlib.sha256(str(ref).encode()).hexdigest()[:12]
        path = self.directory / f"{name}.txt"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return path
