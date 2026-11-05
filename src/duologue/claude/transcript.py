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
from dataclasses import dataclass
from pathlib import Path

from duologue.jsonl import Line

PROJECTS = "projects"
CHAIN_TYPES = ("user", "assistant", "attachment", "system")
MESSAGE_TYPES = ("user", "assistant")
# Stop reasons that end Claude's turn. "tool_use" and "pause_turn" mean it goes on.
FINAL_STOPS = ("end_turn", "stop_sequence", "max_tokens", "refusal")
SYNTHETIC_MODEL = "<synthetic>"


def find_transcript(claude_home: Path, session_id: str) -> Path | None:
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
    dangling: tuple[str, ...]  # tool calls in the last messages that have no result


def tail(lines: list[Line]) -> Tail:
    chain = [ln.record for ln in lines if in_chain(ln.record)]
    messages = [r for r in chain if r.get("type") in MESSAGE_TYPES]
    last = chain[-1] if chain else {}
    # Attachments and some system records carry no folder or version: take the last that does.
    where = next((r for r in reversed(chain) if r.get("cwd")), {})
    model = SYNTHETIC_MODEL
    for r in reversed(messages):
        m = (r.get("message") or {}).get("model")
        if r.get("type") == "assistant" and m and m != SYNTHETIC_MODEL:
            model = m
            break
    return Tail(
        parent_uuid=last.get("uuid"),
        parent_type=messages[-1].get("type") if messages else None,
        model=model,
        version=str(where.get("version") or ""),
        cwd=str(where.get("cwd") or ""),
        git_branch=str(where.get("gitBranch") or ""),
        dangling=dangling_calls(messages),
    )


def dangling_calls(messages: list[dict]) -> tuple[str, ...]:
    """Tool calls after the last user message that no result answers."""
    calls: list[str] = []
    for r in reversed(messages):
        if r.get("type") == "user":
            break
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list):
            calls = [
                b.get("id")
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
            ] + calls
    return tuple(calls)


def settled(lines: list[Line]) -> bool:
    """Whether Claude's last turn has ended: the last message is an assistant message whose stop
    reason ends a turn."""
    for ln in reversed(lines):
        r = ln.record
        if not is_message(r):
            continue
        if r.get("type") != "assistant":
            return False
        return (r.get("message") or {}).get("stop_reason") in FINAL_STOPS
    return True  # nothing has happened in it yet
