"""Claude Code transcripts: where they are and what their end looks like.

A session's transcript is ``<CLAUDE_CONFIG_DIR>/projects/<folder>/<session id>.jsonl``, where
``<folder>`` is the session's working folder with every character other than a letter or digit
replaced by ``-``. ``EnterWorktree`` moves a transcript into the worktree's folder, so a session
is found by its id across all folders, newest file first.

Records link into one chain through ``parentUuid``. Besides user and assistant messages the chain
holds ``attachment`` and ``system`` records; bookkeeping records (``api-request*``,
``last-prompt``, ``queue-operation`` and others) carry no uuid and stay out of it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from duologue.ids import require_id
from duologue.jsonl import Line

PROJECTS = "projects"
CHAIN_TYPES = ("user", "assistant", "attachment", "system")
MESSAGE_TYPES = ("user", "assistant")
# Stop reasons that end Claude's turn. "tool_use" and "pause_turn" mean it goes on.
FINAL_STOPS = ("end_turn", "stop_sequence", "max_tokens", "refusal")
SYNTHETIC_MODEL = "<synthetic>"
# User records Claude Code writes for local slash commands (/model, /cost...): no answer follows.
LOCAL_COMMAND = ("<command-name>", "<local-command-stdout>", "<local-command-stderr>")


def find_transcript(claude_home: Path, session_id: str) -> Path | None:
    require_id(session_id, "session id")
    projects = Path(claude_home) / PROJECTS
    if not projects.is_dir():
        return None
    found = list(projects.glob(f"*/{session_id}.jsonl"))
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def project_folder(cwd: str) -> str:
    """The folder name Claude Code keeps a working folder's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def new_transcript_path(claude_home: Path, cwd: str, session_id: str) -> Path:
    return Path(claude_home) / PROJECTS / project_folder(cwd) / f"{session_id}.jsonl"


def in_chain(record: dict) -> bool:
    return (
        record.get("type") in CHAIN_TYPES
        and bool(record.get("uuid"))
        and not record.get("isSidechain")
    )


def is_message(record: dict) -> bool:
    return in_chain(record) and record.get("type") in MESSAGE_TYPES


@dataclass(frozen=True)
class Tail:
    """What a record appended to the transcript must link to and carry."""

    parent_uuid: str | None  # the last record in the chain
    parent_type: str | None  # the role of the last message: "user" or "assistant"
    model: str  # the last model that answered, or "<synthetic>" before any did
    version: str
    cwd: str
    git_branch: str
    dangling: tuple[str, ...]  # tool calls of the last answer that have no result
    uuids: frozenset[str]  # every uuid in the transcript
    last_assistant: str | None = None  # the uuid of the last assistant message


def tail(lines: Iterable[Line]) -> Tail:
    """The transcript's end, read in one pass over its lines."""
    parent = parent_type = last_assistant = None
    model = SYNTHETIC_MODEL
    where: dict = {}
    calls = Calls()
    uuids: set[str] = set()
    for ln in lines:
        r = ln.record
        if r.get("uuid"):
            uuids.add(r["uuid"])
        if not in_chain(r):
            continue
        parent = r["uuid"]
        if r.get("cwd"):  # attachments and some system records carry no folder or version
            where = r
        if r.get("type") not in MESSAGE_TYPES:
            continue
        parent_type = r["type"]
        if parent_type == "assistant":
            last_assistant = r["uuid"]
        calls.see(r)
        m = (r.get("message") or {}).get("model")
        if r["type"] == "assistant" and m and m != SYNTHETIC_MODEL:
            model = m
    return Tail(
        parent_uuid=parent,
        parent_type=parent_type,
        model=model,
        version=str(where.get("version") or ""),
        cwd=str(where.get("cwd") or ""),
        git_branch=str(where.get("gitBranch") or ""),
        dangling=calls.open(),
        uuids=frozenset(uuids),
        last_assistant=last_assistant,
    )


class Calls:
    """Tool calls waiting for their results, matched by id whatever the order of the records
    (Claude Code writes parallel calls, and their results, one record each)."""

    def __init__(self) -> None:
        self._open: dict[str, None] = {}

    def see(self, record: dict) -> None:
        content = (record.get("message") or {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if record.get("type") == "assistant":
            for b in blocks:
                if b.get("type") == "tool_use" and b.get("id"):
                    self._open[b["id"]] = None
        elif any(b.get("type") == "tool_result" for b in blocks):
            for b in blocks:
                if b.get("type") == "tool_result":
                    self._open.pop(b.get("tool_use_id"), None)
        elif is_typed(record):
            self._open.clear()  # a new prompt: Claude Code settled anything before it

    def open(self) -> tuple[str, ...]:
        return tuple(self._open)


def dangling_calls(messages: Iterable[dict]) -> tuple[str, ...]:
    calls = Calls()
    for r in messages:
        calls.see(r)
    return calls.open()


def is_typed(record: dict) -> bool:
    """A user message that asks something of Claude: not a tool result, meta record, compaction
    summary or local command."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    text = content.strip() if isinstance(content, str) else ""
    return bool(text) and not text.startswith(LOCAL_COMMAND)


def settled(lines: Iterable[Line]) -> bool:
    """Whether Claude's last turn has ended: after the last prompt, an assistant message whose
    stop reason ends a turn is the last message. Meta records, compaction summaries and local
    commands neither start a turn nor need an answer."""
    last = None
    for ln in lines:
        r = ln.record
        if not is_message(r):
            continue
        if r.get("type") == "assistant" or is_typed(r) or _is_result(r):
            last = r
    if last is None:
        return True  # nothing has happened in it yet
    if last.get("type") != "assistant":
        return False
    return (last.get("message") or {}).get("stop_reason") in FINAL_STOPS


def _is_result(record: dict) -> bool:
    content = (record.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )
