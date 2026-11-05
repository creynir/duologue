"""Appending records to a Claude Code transcript, and creating a session.

Callers write only while no Claude Code process has the session (:mod:`duologue.claude.live`).
Before anything is written the new records pass a structure check: Claude Code hangs or drops
messages on a transcript that breaks these rules, so a record that fails is a bug in duologue
and the pair stops instead.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from duologue.claude.transcript import (
    MESSAGE_TYPES,
    dangling_calls,
    in_chain,
    new_transcript_path,
)
from duologue.journal import Blocked, Intent, Journal, Wait, tail_state
from duologue.jsonl import encode, read_lines

THINKING = ("thinking", "redacted_thinking")


@dataclass(frozen=True)
class Written:
    """The outcome of an append: ``[start, end)`` of ``path`` holds the records ``uuids``."""

    path: Path
    start: int
    end: int
    uuids: tuple[str, ...]
    written: bool  # False: the records were already there (a retry), nothing was written


def append_records(path: Path, records: list[dict], *, journal: Journal) -> Written:
    """Append ``records`` to the transcript at ``path``. Raises :class:`Blocked` when they fail
    the structure check or only some of them are there already."""
    size = path.stat().st_size
    ids = tuple(str(r.get("uuid")) for r in records)
    if not records:
        return Written(path, size, size, (), False)
    existing = [ln.record for ln in read_lines(path)]
    have = {r.get("uuid") for r in existing if r.get("uuid")}
    there = [u for u in ids if u in have]
    if there:
        if len(there) == len(ids):
            return Written(path, size, size, ids, False)
        raise Blocked(f"{len(there)} of the {len(ids)} records are already in the Claude session")
    problems = check_structure(existing, records)
    if problems:
        raise Blocked("the records for the Claude session fail the structure check: " + problems[0])
    if size and not _ends_with_newline(path):
        raise Wait("the Claude transcript ends in a line that is still being written")
    intent = Intent("claude", path, size, encode(records), ids)
    journal.begin(intent)
    with open(path, "ab") as fh:
        fh.write(intent.data)
        fh.flush()
        os.fsync(fh.fileno())
    _verify(path, intent)
    return Written(path, size, intent.end, ids, True)


def recover(path: Path, intent: Intent) -> bool:
    """Settle an interrupted append. True when it is all there; False when nothing of it is (a
    cut-off part is taken out again), so the caller can write it again."""
    state = tail_state(path, intent)
    if state == "complete":
        return True
    if state == "absent":
        return False
    if state == "partial":
        os.truncate(path, intent.base)
        return False
    raise Blocked("the Claude transcript changed after an interrupted write by duologue")


def create_session(
    claude_home: Path,
    *,
    cwd: str,
    build: Callable[[str], list[dict]],
    journal: Journal,
    title: str | None = None,
) -> tuple[str, Written]:
    """Create a session in ``cwd`` holding the records ``build(session_id)`` returns, titled
    ``title`` in Claude's session list."""
    session_id = str(uuid.uuid4())
    records = build(session_id)
    problems = check_structure([], records)
    if problems:
        raise Blocked(
            "the records for the new Claude session fail the structure check: " + problems[0]
        )
    extra = []
    if title:
        extra.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    path = new_transcript_path(claude_home, cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = tuple(str(r["uuid"]) for r in records)
    intent = Intent("claude", path, 0, encode(records + extra), ids)
    journal.begin(intent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(intent.data)
        fh.flush()
        os.fsync(fh.fileno())
    _verify(path, intent)
    return session_id, Written(path, 0, intent.end, ids, True)


def check_structure(existing: list[dict], new: list[dict]) -> list[str]:
    """What is wrong with appending ``new`` after ``existing``, in words; empty when nothing is.

    The rules: each record links to the one before it (the first to the transcript's last chain
    record); uuids are new and unique; user and assistant messages strictly alternate; every
    assistant message names its model and carries no thinking; every tool call is answered by
    the next message, whose tool results come before anything else in it and answer exactly the
    calls before them.
    """
    problems: list[str] = []
    chain = [r for r in existing if in_chain(r)]
    messages = [r for r in chain if r.get("type") in MESSAGE_TYPES]
    known = {r.get("uuid") for r in existing if r.get("uuid")}
    parent = chain[-1].get("uuid") if chain else None
    role = messages[-1].get("type") if messages else None
    calls = set(dangling_calls(messages))
    for n, r in enumerate(new):
        where = f"record {n + 1} of {len(new)}"
        u = r.get("uuid")
        if not u or u in known:
            problems.append(f"{where} has a missing or repeated uuid")
        known.add(u)
        if r.get("parentUuid") != parent:
            problems.append(f"{where} does not link to the record before it")
        parent = u
        kind = r.get("type")
        if kind not in MESSAGE_TYPES:
            problems.append(f"{where} is a {kind!r} record, not a message")
            continue
        if kind == role:
            problems.append(f"{where} is a second {kind} message in a row")
        role = kind
        message = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        blocks = [b for b in blocks if isinstance(b, dict)]
        if any(b.get("type") in THINKING for b in blocks):
            problems.append(f"{where} carries thinking, which only Claude can sign")
        if kind == "assistant":
            if not message.get("model"):
                problems.append(f"{where} names no model")
            if calls:
                problems.append(f"{where} follows tool calls that were not answered")
            calls = {b.get("id") for b in blocks if b.get("type") == "tool_use"}
            continue
        kinds = [b.get("type") for b in blocks]
        results = {b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"}
        if results and kinds[: len(results)] != ["tool_result"] * len(results):
            problems.append(f"{where} has tool results after other content")
        if results != calls:
            problems.append(f"{where} does not answer exactly the tool calls before it")
        calls = set()
    if calls:
        problems.append("the last tool call has no result")
    return problems


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def _verify(path: Path, intent: Intent) -> None:
    """Read the write back: the records are there, whole and in order."""
    back = [ln.record.get("uuid") for ln in read_lines(path, intent.base, intent.end)]
    if tuple(u for u in back if u) != intent.ids:
        raise Blocked("the records duologue wrote to the Claude session did not read back")
