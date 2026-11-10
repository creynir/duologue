"""Linking a Claude Code session with a Codex thread, and the other changes a person makes to a
pair: release, resolve and unlink.

Linking two conversations that both exist syncs them from then on: what each held before stays
where it is. Linking one conversation creates the other side from its history, the newest turns
that fit the copy budget, with a note saying where the older ones are.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from duologue.claude import sessions as claude_sessions
from duologue.claude.transcript import (
    PROJECTS,
    find_transcript,
    is_message,
    project_folder,
)
from duologue.codex import threads as codex_threads
from duologue.codex.rollout import (
    SESSIONS,
    anchor_ends,
    batch_anchor,
    batch_end,
    is_compressed,
    session_meta,
    turn_ends,
    uuid7,
)
from duologue.codex.threads import last_turn_open
from duologue.engine import (
    Engine,
    Gone,
    PairJournal,
    _printable,
    claude_start,
    codex_start,
    end_cursor,
)
from duologue.ids import UUID as ID
from duologue.ids import is_id
from duologue.journal import Blocked, Wait, prefix_hash
from duologue.jsonl import Line, iter_lines, read_lines
from duologue.pairs import AutoState, AutoStateError, Cursor, Pair, auto_state_file
from duologue.translate.claude_to_codex import (
    RolloutLine,
    claude_to_codex,
    conversation,
    is_user_input,
    note_turn,
)
from duologue.translate.codex_to_claude import (
    ClaudeStep,
    claude_records,
    codex_to_claude_steps,
    codex_turns,
)
from duologue.translate.common import CODEX_IMPORT_TURN, ORIGIN, text_of

UNLINKED = (
    "unlinked; both conversations stay as they are, and duologue does not pair them again by "
    "itself (link them to sync them again)"
)
NAME = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
LAST = "last"
SCAN_LIMIT = 500  # rollouts looked at to find the newest thread in a folder

T = TypeVar("T")


class LinkError(Exception):
    """The link cannot be made as asked."""


class NoSuchPair(LinkError):
    """No pair has that name or id."""


class NotImported(LinkError):
    """The thread Codex's importer made no longer matches the Claude session. ``continued``:
    the user carried on in that thread in Codex."""

    def __init__(self, text: str, continued: bool):
        super().__init__(text)
        self.continued = continued


# ----------------------------------------------------------------------------------- finding
def find_claude(engine: Engine, value: str, cwd: str) -> str:
    """A Claude session id: ``value`` itself, or with ``last`` the newest session in ``cwd``."""
    home = engine.config.claude_home
    if value == LAST:
        folder = Path(home) / PROJECTS / project_folder(cwd)
        found = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in found:
            if ID.fullmatch(path.stem):
                return path.stem
        raise LinkError(f"no Claude Code session found for {cwd}")
    if not ID.fullmatch(value) or find_transcript(home, value) is None:
        raise LinkError(f"no Claude Code session {value!r} in {home}")
    return value


def find_codex(engine: Engine, value: str, cwd: str) -> str:
    """A Codex thread id: ``value`` itself, or with ``last`` the newest thread started in
    ``cwd``."""
    if value == LAST:  # an archived or compressed thread is not one the user is working in
        folder = Path(engine.config.codex_home) / SESSIONS
        found = [p for p in folder.rglob("rollout-*.jsonl") if not is_compressed(p)]
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        want = os.path.realpath(cwd)
        for path in found[:SCAN_LIMIT]:
            meta = session_meta(path)
            if meta.get("cwd") and os.path.realpath(meta["cwd"]) == want and is_id(meta.get("id")):
                return str(meta["id"])
        raise LinkError(f"no Codex thread found for {cwd}")
    if not ID.fullmatch(value) or engine.codex.rollout(value) is None:
        raise LinkError(f"no Codex thread {value!r} in {engine.config.codex_home}")
    return value


# ------------------------------------------------------------------------------------- link
def link(
    engine: Engine,
    *,
    claude: str | None,
    codex: str | None,
    name: str | None,
    cwd: str,
) -> tuple[Pair, str]:
    """Link the two (or create the missing one) and add the pair. Returns it and what was done."""
    if not claude and not codex:
        raise LinkError("name a Claude session (--claude) or a Codex thread (--codex), or both")
    claude = find_claude(engine, claude, cwd) if claude else None
    codex = find_codex(engine, codex, cwd) if codex else None
    for p in engine.pairs.values():
        if claude and p.claude == claude:
            raise LinkError(f'that Claude session is already linked, as "{p.name}"')
        if codex and p.codex == codex:
            raise LinkError(f'that Codex thread is already linked, as "{p.name}"')
    if name is not None and not NAME.fullmatch(name):
        raise LinkError("a name is 1 to 80 characters on one line")
    if claude and codex:
        pair = Pair(
            name=_unique(engine, name or _claude_title(engine, claude)), claude=claude, codex=codex
        )
        pair.claude_cursor = claude_start(find_transcript(engine.config.claude_home, claude))
        pair.codex_cursor = codex_start(engine.codex_path(pair))
        done = "linked; turns from now on are copied both ways"
    elif claude:
        pair, done = _codex_from_claude(engine, claude, name)
    else:
        pair, done = _claude_from_codex(engine, codex, name)
    engine.pairs[pair.name] = pair
    engine.save()
    engine.log.write(pair.name, "link", done)
    try:  # linked by hand: the user wants them paired again
        AutoState.load(auto_state_file(engine.config)).unignore(pair.claude, pair.codex)
    except (AutoStateError, OSError) as e:
        engine.log.write(pair.name, "error", f"linked, but auto.json was not updated: {e}")
    return pair, done


def _codex_from_claude(engine: Engine, claude: str, name: str | None) -> tuple[Pair, str]:
    engine.codex_gate()
    cpath = find_transcript(engine.config.claude_home, claude)
    start = claude_start(cpath)
    records = [ln.record for ln in read_lines(cpath, 0, start.end)]
    cwd = next((r["cwd"] for r in reversed(records) if r.get("cwd")), None) or os.getcwd()
    thread_id = uuid7()
    pair = Pair(
        name=_unique(engine, name or _claude_title(engine, claude)), claude=claude, codex=thread_id
    )
    pair.claude_cursor = start
    key, kept = pair.key, {}

    def build(tid: str) -> list[RolloutLine]:
        def translate(turn: list[dict]) -> list[RolloutLine]:
            return claude_to_codex(
                turn, thread_id=tid, key=key, outputs=engine.outputs, default_cwd=cwd
            )

        turns, dropped = newest_lazily(
            _claude_turns(records), translate, _model_size, engine.config.copy_budget
        )
        kept["turns"], kept["dropped"] = len(turns), dropped
        if not turns:
            raise LinkError("the Claude session has nothing to copy yet")
        lines = [ln for turn in turns for ln in turn]
        if dropped:
            note = (
                f"this thread continues a Claude Code session; its {dropped} earlier turn(s) "
                f"are not copied here. They are in {cpath}"
            )
            lines = note_turn(note, thread_id=tid, key=key, ms=lines[0].ms - 1) + lines
        return lines

    after_claude = {"end": start.end, "anchor": start.anchor}
    made = _create(
        engine,
        pair,
        PairJournal(engine, pair, claude=after_claude, codex=None),
        lambda journal: codex_threads.create_thread(
            engine.codex,
            cwd=cwd,
            build=build,
            journal=journal,
            source=engine.config.codex_source,
            thread_id=thread_id,
        )[1],
    )
    engine.confirm(pair, after_claude, {"end": made.end, "anchor": made.turns[-1]})
    _source_unchanged(engine, pair, cpath, start, "Claude session")
    done = f"created Codex thread {thread_id} with {kept['turns']} turn(s) from Claude"
    if kept["dropped"]:
        done += f" ({kept['dropped']} older turn(s) left out: over the copy budget)"
    return pair, done


def _claude_from_codex(engine: Engine, codex: str, name: str | None) -> tuple[Pair, str]:
    xpath = engine.codex_path(Pair(name="", claude="", codex=codex))
    if session_meta(xpath).get("history_base"):
        # Its earlier turns are in the file it was rewound from; copying only the rest would
        # look like the whole conversation.
        raise LinkError(
            "the Codex thread was rewound in Codex, and duologue cannot copy its history yet; "
            "link it to an existing Claude session instead"
        )
    lines = read_lines(xpath)
    ends = turn_ends(lines)
    if not ends:
        raise LinkError("the Codex thread has no finished turn to copy yet")
    end = batch_end(lines)  # after the last turn, or a rollback that follows it
    finished = [ln for ln in lines if ln.end <= end]
    # The turns Codex still holds: a rollback drops earlier turns, as Codex rebuilds the thread.
    groups = [g for _, g in codex_turns([ln.record for ln in finished])[0]]
    cwd = session_meta(xpath).get("cwd") or os.getcwd()
    session_id = str(uuid.uuid4())
    pair = Pair(
        name=_unique(engine, name or _codex_title(finished)), claude=session_id, codex=codex
    )
    version = claude_version(engine.config.claude_bin)
    key, kept = pair.key, {}

    def build(sid: str) -> list[dict]:
        def translate(group: list[dict]) -> list[ClaudeStep]:
            return codex_to_claude_steps(group, key=key, outputs=engine.outputs)

        turns, dropped = newest_lazily(groups, translate, _steps_size, engine.config.copy_budget)
        kept["turns"], kept["dropped"] = len(turns), dropped
        if not turns:
            raise LinkError("the Codex thread has nothing to copy yet")
        extra = ""
        if dropped:
            extra = (
                f"This session continues a Codex thread; its {dropped} earlier turn(s) are not "
                f"copied here. They are in {xpath}."
            )
        return claude_records(
            [s for t in turns for s in t],
            session_id=sid,
            parent_uuid=None,
            parent_type=None,
            model="<synthetic>",
            version=version,
            cwd=cwd,
            lead_extra=extra,
        )

    after_codex = {"end": end, "anchor": batch_anchor(finished)}
    before_digest = prefix_hash(xpath, end)
    made = _create(
        engine,
        pair,
        PairJournal(engine, pair, claude=None, codex=after_codex),
        lambda journal: claude_sessions.create_session(
            engine.config.claude_home,
            cwd=cwd,
            build=build,
            journal=journal,
            title=pair.name,
            session_id=session_id,
        )[1],
    )
    engine.confirm(pair, {"end": made.end, "anchor": made.uuids[-1]}, after_codex)
    source = Cursor(end, before_digest, after_codex["anchor"])
    _source_unchanged(engine, pair, xpath, source, "Codex thread")
    done = f"created Claude session {session_id} with {kept['turns']} turn(s) from Codex"
    if kept["dropped"]:
        done += f" ({kept['dropped']} older turn(s) left out: over the copy budget)"
    return pair, done


def link_import(engine: Engine, claude: str, codex: str, sha256: str) -> tuple[Pair, str]:
    """Pair a Claude session with the thread Codex's own importer made of it. What Codex copied
    is not copied again: the Claude side syncs on from the point Codex copied it at (where its
    file had the sha256 Codex recorded), the Codex side from after the importer's last turn.
    Claude's turns since then travel on the next pass, as many as fit the copy budget (newest
    first), after a note saying where the others are. Raises :class:`NotImported` when the
    Claude session no longer starts with what Codex copied."""
    for p in engine.pairs.values():
        if claude in (p.claude, p.codex) or codex in (p.claude, p.codex):
            raise LinkError(f'that conversation is already linked, as "{p.name}"')
    cpath = find_transcript(engine.config.claude_home, claude)
    if cpath is None:
        raise LinkError(f"no Claude Code session {claude!r} in {engine.config.claude_home}")
    pair = Pair(name=_unique(engine, _claude_title(engine, claude)), claude=claude, codex=codex)
    xpath = engine.codex_path(pair)
    lines = read_lines(xpath)
    if session_meta(xpath).get("history_base"):  # its imported turns are in another file
        raise NotImported("the Codex thread was rewound in Codex", continued=False)
    copied = _prefix_with_digest(cpath, sha256)
    if copied is None:
        continued = any(not t.startswith(CODEX_IMPORT_TURN) for t, _ in turn_ends(lines))
        raise NotImported("the Claude session changed since Codex imported it", continued)
    pair.claude_cursor, dropped = _newest_since(engine, pair, cpath, copied)
    if dropped:
        pair.note_to_codex = (
            f"the Claude Code session goes on after the turns imported above; its next {dropped} "
            f"turn(s) are not copied here. They are in {cpath}"
        )
    ends = [(turn, end) for turn, end in turn_ends(lines) if turn.startswith(CODEX_IMPORT_TURN)]
    if ends:
        turn, end = ends[-1]
        pair.codex_cursor = Cursor(end, prefix_hash(xpath, end), turn)
    else:
        pair.codex_cursor = codex_start(xpath)
    engine.pairs[pair.name] = pair
    engine.save()
    done = "paired with the thread Codex's importer made of it"
    if dropped:
        done += f" ({dropped} Claude turn(s) since the import left out: over the copy budget)"
    engine.log.write(pair.name, "link", done)
    return pair, done


def _newest_since(engine: Engine, pair: Pair, cpath: Path, copied: Cursor) -> tuple[Cursor, int]:
    """Where to sync Claude on from after ``copied``: the start of the newest turns that fit the
    copy budget (at least one), and how many turns before them are left out."""
    lines = read_lines(cpath, copied.end)
    parts: list[list[Line]] = []
    for ln in lines:  # split where the user typed (a queued message is not a user record)
        if is_user_input(ln.record) or not parts:
            parts.append([])
        parts[-1].append(ln)
    cwd = next((ln.record["cwd"] for ln in reversed(lines) if ln.record.get("cwd")), "")
    total, first, kept = 0, len(parts), False
    for i in range(len(parts) - 1, -1, -1):
        made = claude_to_codex(
            [ln.record for ln in parts[i]],
            thread_id=pair.codex,
            key=pair.key,
            outputs=engine.outputs,
            default_cwd=cwd,
            skip_ours=True,
        )
        if not made:
            continue
        size = _model_size(made)
        if kept and total + size > engine.config.copy_budget:
            break
        total, first, kept = total + size, i, True
    if first == 0 or first == len(parts):
        return copied, 0
    # Turns the user typed (duologue's own copies from Codex are not sent back, so not counted).
    dropped = sum(
        1 for part in parts[:first] if is_user_input(r := part[0].record) and not r.get(ORIGIN)
    )
    end = parts[first - 1][-1].end
    anchor = (
        next(
            (ln.record["uuid"] for ln in reversed(parts[first - 1]) if ln.record.get("uuid")), None
        )
        or copied.anchor
    )
    return Cursor(end, prefix_hash(cpath, end), anchor), dropped


def _prefix_with_digest(path: Path, sha256: str) -> Cursor | None:
    """A cursor at the end of the line where the file's first bytes hash to ``sha256``."""
    h = hashlib.sha256()
    end, anchor = 0, None
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            h.update(raw)
            end += len(raw)
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            if isinstance(record, dict) and record.get("uuid"):
                anchor = record["uuid"]
            if h.hexdigest() == sha256:
                return Cursor(end, sha256, anchor)
    return None


def _source_unchanged(engine: Engine, pair: Pair, path: Path, cur: Cursor, what: str) -> None:
    """Block the new pair when the conversation it was copied from was rewritten meanwhile:
    the copy may not match it, so a person decides (``duologue release``)."""
    if path.stat().st_size >= cur.end and prefix_hash(path, cur.end) == cur.digest:
        return
    engine.block(pair, f"the {what} changed while duologue copied it; check the new side")
    engine.save()


def _create(engine: Engine, pair: Pair, journal: PairJournal, create):
    """Create the missing side with the pair recorded first, so an interrupted create is
    settled by the next sync like any other write. If nothing was written, the pair goes."""
    engine.pairs[pair.name] = pair
    try:
        return create(journal)
    except BaseException as e:
        p = pair.pending
        if p is None or not Path(p.path).exists():
            engine.forget_pending(pair)
            engine.pairs.pop(pair.name, None)
            engine.save()
            if isinstance(e, (Wait, Blocked)):
                raise LinkError(str(e)) from e
            raise
        engine.save()
        if isinstance(e, Exception):
            raise LinkError(
                f"creating the other side was interrupted ({e}); duologue sync finishes it"
            ) from e
        raise


# ---------------------------------------------------------------------------- the other verbs
def release(engine: Engine, pair: Pair) -> str:
    """Take a pair off hold, or unblock it. A write that was never confirmed is settled, or,
    when that is what blocked the pair, accepted as it stands; a side rewritten under duologue
    syncs on from where it stands now (its anchor if that is still there, else its end)."""
    said = []
    if pair.hold:
        pair.hold = None
        said.append("taken off hold")
    if pair.blocked:
        pair.blocked = None
        said.append("unblocked")
        if pair.pending:
            try:
                engine.settle(pair)
            except Blocked:
                engine.accept_pending(pair)
                said.append("the interrupted write was settled as the files stand")
        try:
            cpath, xpath = engine.paths(pair)
        except Gone as e:
            raise LinkError(f"{e}; the pair is removed by itself within the hour") from e
        if not _cursor_matches(cpath, pair.claude_cursor):
            pair.claude_cursor = _reanchor_claude(cpath, pair.claude_cursor)
            said.append("the Claude side syncs on from where it stands")
        if not _cursor_matches(xpath, pair.codex_cursor):
            pair.codex_cursor = _reanchor_codex(xpath, pair.codex_cursor)
            said.append("the Codex side syncs on from where it stands")
    pair.seen, pair.checked = [], {}
    text = "; ".join(said) or "nothing to release"
    engine.save()
    engine.log.write(pair.name, "release", text)
    return text


def resolve(engine: Engine, pair: Pair, keep: str) -> str:
    """End a conflict by keeping one side's new turns; the other side's new turns are not
    copied (they stay in that app)."""
    if keep not in ("claude", "codex"):
        raise LinkError("keep claude or codex")
    if not pair.conflict:
        if engine.config.on_conflict != "stop":
            return (
                "there is no conflict to resolve: both sides' turns are kept "
                '(on_conflict = "stop" waits for resolve instead)'
            )
        return "there is no conflict to resolve"
    cpath, xpath = engine.paths(pair)
    if keep == "claude":
        cur = engine.codex_cursor(pair, xpath)  # finds the synced turn again after a rewrite
        if last_turn_open(xpath):
            raise LinkError("a Codex turn is still running; resolve once it has finished")
        lines = read_lines(xpath, cur.end)
        end = batch_end(lines)
        if end is not None:
            pair.codex_cursor = Cursor(end, prefix_hash(xpath, end), batch_anchor(lines))
        text = "keeping Claude's new turns; Codex's new turns stay in Codex only"
    else:
        engine.claude_cursor(pair, cpath)
        pair.claude_cursor = end_cursor(cpath)  # all of Claude's new records, answered or not
        text = "keeping Codex's new turns; Claude's new turns stay in Claude only"
    pair.conflict = False
    pair.seen, pair.checked = [], {}
    engine.save()
    engine.log.write(pair.name, "resolve", text)
    return text


def unlink(engine: Engine, pair: Pair, force: bool = False) -> str:
    if pair.pending and not force:
        raise LinkError(
            "a write to this pair is not confirmed yet; run duologue sync first, or unlink "
            "with --force"
        )
    # Not paired again by itself: the user decided (linking them again pairs them). Recorded
    # first, so the pair is never gone without its decision.
    AutoState.load(auto_state_file(engine.config)).ignore(pair.claude, pair.codex)
    engine.forget_pending(pair)
    del engine.pairs[pair.name]
    engine.save()
    engine.log.write(pair.name, "unlink", UNLINKED)
    return UNLINKED


def find_pair(engine: Engine, ref: str) -> Pair:
    """A pair by its name, a unique start of its name, or either side's id."""
    if ref in engine.pairs:
        return engine.pairs[ref]
    by_id = [p for p in engine.pairs.values() if ref in (p.claude, p.codex)]
    if by_id:
        return by_id[0]
    starts = [p for p in engine.pairs.values() if p.name.lower().startswith(ref.lower())]
    if len(starts) == 1:
        return starts[0]
    if starts:
        raise LinkError(f"{ref!r} matches more than one pair: " + ", ".join(p.name for p in starts))
    raise NoSuchPair(f"no pair {ref!r}; duologue status lists them")


# ------------------------------------------------------------------------------------ helpers
def newest(groups: Sequence[T], size: Callable[[T], int], budget: int) -> tuple[list[T], int]:
    """The newest groups whose sizes add up to at most ``budget`` (at least one), and how many
    older ones are left out."""
    return newest_lazily(groups, lambda g: g, size, budget)


U = TypeVar("U")


def newest_lazily(
    groups: Sequence[T], make: Callable[[T], U], size: Callable[[U], int], budget: int
) -> tuple[list[U], int]:
    """Like :func:`newest`, making each group only when it is reached, newest first: groups
    over the budget are never made (translating writes long outputs to files)."""
    kept: list[U] = []
    total = 0
    skipped = 0
    for g in reversed(groups):
        made = make(g)
        if not made:  # nothing in it to copy (an aborted turn, say): not kept, not left out
            skipped += 1
            continue
        s = size(made)
        if kept and total + s > budget:
            break
        kept.append(made)
        total += s
    kept.reverse()
    return kept, len(groups) - len(kept) - skipped


def claude_version(claude_bin: str) -> str:
    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\d+\.\d+\.\d+", out)
    return match.group(0) if match else ""


def _claude_turns(records: list[dict]) -> list[list[dict]]:
    """Claude records split where the user typed, each part one Codex turn (records before the
    first prompt make a part of their own)."""
    parts: list[list[dict]] = []
    for r in conversation(records):
        # A message queued while Claude worked joins the running turn, as it does in sync.
        if (is_user_input(r) and not r.get("queued")) or not parts:
            parts.append([])
        parts[-1].append(r)
    return parts


def _model_size(turn: list[RolloutLine]) -> int:
    """What a turn costs the Codex model: its model-visible lines."""
    return sum(
        len(json.dumps(ln.payload, ensure_ascii=False)) for ln in turn if ln.type == "response_item"
    )


def _steps_size(steps: list[ClaudeStep]) -> int:
    return sum(len(json.dumps(s.blocks, ensure_ascii=False)) for s in steps)


def _cursor_matches(path: Path, cur: Cursor) -> bool:
    return path.stat().st_size >= cur.end and prefix_hash(path, cur.end) == cur.digest


def _reanchor_claude(cpath: Path, cur: Cursor) -> Cursor:
    for ln in iter_lines(cpath):
        if cur.anchor and ln.record.get("uuid") == cur.anchor:
            return Cursor(ln.end, prefix_hash(cpath, ln.end), cur.anchor)
    return claude_start(cpath)


def _reanchor_codex(xpath: Path, cur: Cursor) -> Cursor:
    hits = anchor_ends(iter_lines(xpath), cur.anchor) if cur.anchor else []
    if hits:
        return Cursor(hits[-1], prefix_hash(xpath, hits[-1]), cur.anchor)
    return codex_start(xpath)


def _unique(engine: Engine, name: str) -> str:
    name = " ".join(_printable(name).split())[:80] or "conversation"
    if name not in engine.pairs:
        return name
    n = 2
    while f"{name} ({n})" in engine.pairs:
        n += 1
    return f"{name} ({n})"


def _claude_title(engine: Engine, claude: str) -> str:
    title, first = None, None
    for ln in iter_lines(find_transcript(engine.config.claude_home, claude)):
        r = ln.record
        if r.get("type") == "custom-title" and r.get("customTitle"):
            title = r["customTitle"]
        elif r.get("type") == "ai-title" and r.get("aiTitle") and not title:
            title = r["aiTitle"]
        elif first is None and is_message(r) and is_user_input(r):
            first = text_of((r.get("message") or {}).get("content"))
    return _shorten(title or first or "conversation")


def _codex_title(lines: list[Line]) -> str:
    for ln in lines:
        p = ln.record.get("payload")
        item = p.get("item") if isinstance(p, dict) else None
        if isinstance(item, dict) and item.get("type") == "UserMessage":
            text = "\n".join(
                c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)
            )
            if text.strip():
                return _shorten(text)
    return "conversation"


def _shorten(text: str) -> str:
    line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else "conversation"
    return line if len(line) <= 60 else line[:59] + "…"
