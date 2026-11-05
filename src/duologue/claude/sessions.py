"""Appending records to a Claude Code transcript, and creating a session.

Records are written only while no Claude Code process has the session: the caller passes a
``live`` check that runs just before the write and again just after it. Before anything is
written the new records pass a structure check: Claude Code hangs or drops messages on a
transcript that breaks these rules, so records that fail are a bug in duologue and the pair
stops instead.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from duologue.claude.live import UNKNOWN
from duologue.claude.transcript import MESSAGE_TYPES, Tail, new_transcript_path, tail
from duologue.ids import is_id
from duologue.journal import Blocked, Intent, Journal, Wait, prefix_hash, tail_state
from duologue.jsonl import encode, ends_with_newline, iter_lines, read_lines

THINKING = ("thinking", "redacted_thinking")

Live = Callable[[], "int | None"]  # the pid of a Claude Code that has the session, or None


@dataclass(frozen=True)
class Written:
    """The outcome of an append: ``[start, end)`` of ``path`` holds the records ``uuids``."""

    path: Path
    start: int
    end: int
    uuids: tuple[str, ...]
    written: bool  # False: the records were already there (a retry), nothing was written


def append_records(
    path: Path, records: list[dict], *, journal: Journal, live: Live | None = None
) -> Written:
    """Append ``records`` to the transcript at ``path``. Raises :class:`Wait` when Claude has the
    session open or the transcript moved on since the records were built, and
    :class:`Blocked` when the records fail the structure check or only some of them are there."""
    ids = tuple(str(r.get("uuid")) for r in records)
    size = path.stat().st_size
    if not records:
        return Written(path, size, size, (), False)
    end = tail(iter_lines(path))
    there = [u for u in ids if u in end.uuids]
    if there:
        if len(there) == len(ids):
            start, stop = _record_range(path, ids)
            _same_records(path, start, stop, records)
            return Written(path, start, stop, ids, False)
        raise Blocked(f"{len(there)} of the {len(ids)} records are already in the Claude session")
    if records[0].get("parentUuid") != end.parent_uuid:
        raise Wait("the Claude transcript moved on; duologue builds its records again")
    problems = check_structure(end, records)
    if problems:
        raise Blocked("the records for the Claude session fail the structure check: " + problems[0])
    if not ends_with_newline(path):
        raise Wait("the Claude transcript ends in a line that is still being written")
    _not_live(live)
    base = path.stat().st_size
    intent = Intent("claude", path, base, encode(records), ids, prefix_hash(path, base))
    if intent.base != size:
        raise Wait("the Claude transcript moved on; duologue builds its records again")
    journal.begin(intent)
    _append(path, intent)
    if live is not None and live() is not None:
        # Claude opened the session meanwhile: its next message would leave these records on
        # a side branch. Take them out while they are still the last thing in the file.
        if tail_state(path, intent) == "complete":
            os.truncate(path, intent.base)
            raise Wait("Claude opened the session while duologue wrote; it tries again later")
        raise Blocked("Claude opened the session while duologue wrote to it; check the session")
    _verify(path, intent)
    return Written(path, intent.base, intent.end, ids, True)


def recover(path: Path, intent: Intent) -> bool:
    """Settle an interrupted append. True when it is all there; False when nothing of it is (a
    cut-off part is taken out again), so the caller can write it again."""
    state = tail_state(path, intent)
    if state in ("complete", "followed"):
        return True
    if state == "absent":
        return False
    if state == "partial":
        _take_out(path, intent)
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
    problems = check_structure(tail([]), records)
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
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(intent.data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _verify(path, intent)
    return session_id, Written(path, 0, intent.end, ids, True)


def check_structure(end: Tail, new: list[dict]) -> list[str]:
    """What is wrong with appending ``new`` to a transcript that ends as ``end``, in words; empty
    when nothing is.

    The rules: each record links to the one before it (the first to the transcript's last chain
    record); uuids are new and unique; user and assistant messages alternate, except that tool
    results Claude still owes may follow the last user message; every assistant message names
    its model and carries no thinking; every tool call is answered by the next message, whose
    tool results come before anything else in it and answer exactly the calls still open.
    """
    problems: list[str] = []
    known = set(end.uuids)
    parent, role, calls = end.parent_uuid, end.parent_type, set(end.dangling)
    for n, r in enumerate(new):
        where = f"record {n + 1} of {len(new)}"
        u = r.get("uuid")
        if not is_id(u) or u in known:
            problems.append(f"{where} has a missing, malformed or repeated uuid")
        known.add(u)
        if r.get("parentUuid") != parent:
            problems.append(f"{where} does not link to the record before it")
        parent = u
        kind = r.get("type")
        if kind not in MESSAGE_TYPES:
            problems.append(f"{where} is a {kind!r} record, not a message")
            continue
        if kind == role and not (kind == "user" and calls):
            problems.append(f"{where} is a second {kind} message in a row")
        role = kind
        message = r.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list) and all(isinstance(b, dict) for b in content):
            blocks = content
        else:
            problems.append(f"{where} has no message content Claude can read")
            continue
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
        results = [b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"]
        if results and kinds[: len(results)] != ["tool_result"] * len(results):
            problems.append(f"{where} has tool results after other content")
        if len(set(results)) != len(results) or set(results) != calls:
            problems.append(f"{where} does not answer exactly the tool calls before it, once each")
        calls = set()
    if calls:
        problems.append("the last tool call has no result")
    return problems


def _not_live(live: Live | None) -> None:
    pid = live() if live is not None else None
    if pid == UNKNOWN:
        raise Wait("the Claude session may be open (the process list could not be read)")
    if pid is not None:
        raise Wait(f"the Claude session is open (process {pid})")


def _append(path: Path, intent: Intent) -> None:
    """Append the intent's bytes at ``base``; a write that fails partway is taken out again."""
    with open(path, "ab") as fh:
        try:
            if fh.tell() != intent.base:
                raise Wait("the Claude transcript moved on; duologue builds its records again")
            fh.write(intent.data)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            if tail_state(path, intent) == "partial":
                os.truncate(path, intent.base)
            raise


def _take_out(path: Path, intent: Intent) -> None:
    if intent.base == 0:
        path.unlink(missing_ok=True)
    else:
        os.truncate(path, intent.base)


def _record_range(path: Path, ids: Iterable[str]) -> tuple[int, int]:
    """Where records ``ids`` are in the transcript: from the start of the first line to the end
    of the last."""
    ids = list(ids)
    start = end = None
    previous = 0
    for ln in iter_lines(path):
        u = ln.record.get("uuid")
        if u == ids[0] and start is None:
            start = previous
        if u == ids[-1]:
            end = ln.end
        previous = ln.end
    if start is None or end is None:
        raise Blocked("the records already in the Claude session are not whole")
    return start, end


def _same_records(path: Path, start: int, stop: int, records: list[dict]) -> None:
    """A retry finds its records by uuid; their messages must be the ones it would write."""
    there = {ln.record.get("uuid"): ln.record for ln in iter_lines(path, start, stop)}
    for r in records:
        if there.get(r["uuid"], {}).get("message") != r.get("message"):
            raise Blocked("the Claude session holds records with duologue's ids but other content")


def _verify(path: Path, intent: Intent) -> None:
    """Read the write back: the records are there, whole and in order."""
    back = [ln.record.get("uuid") for ln in read_lines(path, intent.base, intent.end)]
    if tuple(u for u in back if u) != intent.ids:
        raise Blocked("the records duologue wrote to the Claude session did not read back")
