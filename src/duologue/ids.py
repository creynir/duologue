"""The ids both apps give conversations: UUIDs. Every id that goes into a file path or a glob
pattern is checked first, so a malformed one cannot name another file."""

from __future__ import annotations

import re

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def is_id(value: object) -> bool:
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def require_id(value: object, what: str = "id") -> str:
    if not is_id(value):
        raise ValueError(f"{what} {value!r} is not a UUID")
    return value  # type: ignore[return-value]
