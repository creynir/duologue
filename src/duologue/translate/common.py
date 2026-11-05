"""Helpers both translation directions share."""

from __future__ import annotations

import difflib
import json
import os
import uuid
from datetime import UTC, datetime

# Tags duologue's own notes, so a note never travels back to the side it came from.
MARK = "⇄ duologue"
NOTE_PREFIX = f"[{MARK}:"  # every note duologue writes starts with this

# Every id duologue writes derives from this namespace and the source record's id.
NAMESPACE = uuid.UUID("33315e20-742e-4a7d-90d3-56d6e041de3a")

# Codex injects these as user-role context. They are not the user's words.
CODEX_INJECTED = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<turn_aborted>",
    "<skills_instructions>",
    "<world_state>",
    "<user_shell_command>",
    "<recommended_plugins>",
    "<external_codex_apps",
)
# Codex's own importer marks the history it imported with this line.
CODEX_IMPORT_SENTINEL = "<EXTERNAL SESSION IMPORTED>"


def stable_id(*parts: object) -> uuid.UUID:
    """A uuid5 derived from ``parts``: the same parts always give the same id."""
    return uuid.uuid5(NAMESPACE, ":".join(str(p) for p in parts))


def epoch_ms(timestamp: object) -> int | None:
    """An ISO-8601 timestamp (``Z`` or offset) as epoch milliseconds, or None."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def iso_ms(ms: int) -> str:
    """Epoch milliseconds as the ISO-8601 form both CLIs write, e.g. 2026-11-04T16:07:09.123Z."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def text_of(content: object) -> str:
    """The text of a Claude message's content: a plain string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def tool_result_text(content: object) -> str:
    """The text of a Claude ``tool_result`` block's content; images become a placeholder."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[an image]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def unified_diff(path: str, old: str | None, new: str | None) -> str:
    """A unified diff of one file's change, headed with the file's base name."""
    name = os.path.basename(path)
    return "".join(difflib.unified_diff(_lines(old), _lines(new), f"a/{name}", f"b/{name}"))


def _lines(text: str | None) -> list[str]:
    # Every line ends in a newline, or a last line without one would run into the next diff line.
    return [ln if ln.endswith("\n") else ln + "\n" for ln in (text or "").splitlines(True)]


def is_note(text: str) -> bool:
    """A note duologue wrote. Notes start with ``[⇄ duologue:``; the mark elsewhere in a text, such
    as an agent quoting a note, does not make it one."""
    return text.lstrip().startswith(NOTE_PREFIX)


def is_codex_noise(text: str) -> bool:
    """Text Codex put in a user-role message that the user did not type, or one of our notes."""
    s = text.strip()
    return not s or s.startswith(CODEX_INJECTED) or is_note(s) or CODEX_IMPORT_SENTINEL in s
