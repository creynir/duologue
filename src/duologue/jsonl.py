"""Reading JSON-lines files the way both apps write them: whole lines only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Line:
    """One whole line's record and the byte offset just after the line."""

    record: dict
    end: int


def read_lines(path: Path, start: int = 0, end: int | None = None) -> list[Line]:
    """The whole lines between byte ``start`` and ``end`` (default: the end of the file). A last
    line without its newline is still being written and is left out; so are lines that do not
    parse as a JSON object."""
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read() if end is None else fh.read(max(0, end - start))
    out, offset = [], start
    for raw in data.split(b"\n")[:-1]:  # what follows the last newline is not a whole line
        offset += len(raw) + 1
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(Line(record, offset))
    return out


def encode(records: list[dict]) -> bytes:
    """Records as JSON lines, non-ASCII kept as it is."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode()
