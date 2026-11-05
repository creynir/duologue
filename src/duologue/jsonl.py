"""Reading JSON-lines files the way both apps write them: whole lines only, one at a time."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# A UTF-16 surrogate without its pair (Claude Code can cut a long line inside an emoji). Neither
# app's reader accepts one, so text duologue writes replaces it.
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class Line:
    """One whole line's record and the byte offset just after the line."""

    record: dict
    end: int


def iter_lines(path: Path, start: int = 0, end: int | None = None) -> Iterator[Line]:
    """The whole lines between byte ``start`` (where a line starts) and ``end`` (default: the
    end of the file), read one at a time. A last line without its newline is still being
    written and is left out; so are lines that do not parse as a JSON object."""
    with open(path, "rb") as fh:
        if start:
            fh.seek(start - 1)
            if fh.read(1) != b"\n":
                raise ValueError(f"{path}: byte {start} is not the start of a line")
        fh.seek(start)
        offset = start
        for raw in fh:
            if not raw.endswith(b"\n"):
                return
            if end is not None and offset + len(raw) > end:
                return
            offset += len(raw)
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield Line(record, offset)


def read_lines(path: Path, start: int = 0, end: int | None = None) -> list[Line]:
    return list(iter_lines(path, start, end))


def ends_with_newline(path: Path) -> bool:
    """Whether the file's last line is whole (an empty file counts as whole)."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        if fh.tell() == 0:
            return True
        fh.seek(-1, 2)
        return fh.read(1) == b"\n"


def clean(text: str) -> str:
    """``text`` with surrogate pairs joined into the characters they stand for and lone
    surrogates replaced by U+FFFD, so it encodes as UTF-8."""
    if not LONE_SURROGATE.search(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def dumps(record: dict, compact: bool = False) -> str:
    separators = (",", ":") if compact else None
    return clean(json.dumps(record, ensure_ascii=False, separators=separators))


def encode(records: list[dict], compact: bool = False) -> bytes:
    """Records as JSON lines, non-ASCII kept as it is."""
    return "".join(dumps(r, compact) + "\n" for r in records).encode()
