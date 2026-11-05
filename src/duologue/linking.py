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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from duologue.claude import sessions as claude_sessions
from duologue.claude.transcript import (
    PROJECTS,
    find_transcript,
    is_message,
    project_folder,
    settled,
)
from duologue.codex import threads as codex_threads
from duologue.codex.rollout import ARCHIVED, SESSIONS, session_meta, turn_ends
from duologue.engine import Engine
from duologue.ids import UUID as ID
from duologue.journal import MemoryJournal, prefix_hash
from duologue.jsonl import Line, read_lines
from duologue.pairs import Cursor, Pair
from duologue.translate.claude_to_codex import (
    RolloutLine,
    claude_to_codex,
    is_user_input,
    note_turn,
)
from duologue.translate.codex_to_claude import ClaudeStep, claude_records, codex_to_claude_steps
from duologue.translate.common import text_of

NAME = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
LAST = "last"
SCAN_LIMIT = 500  # rollouts looked at to find the newest thread in a folder

T = TypeVar("T")


class LinkError(Exception):
    """The link cannot be made as asked."""


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
    if value == LAST:
        home = Path(engine.config.codex_home)
        found = []
        for folder in (SESSIONS, ARCHIVED):
            if (home / folder).is_dir():
                found += (home / folder).rglob("rollout-*.jsonl")
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        want = os.path.realpath(cwd)
        for path in found[:SCAN_LIMIT]:
            meta = session_meta(path)
            if meta.get("cwd") and os.path.realpath(meta["cwd"]) == want and meta.get("id"):
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
        pair.claude_cursor = _claude_start(find_transcript(engine.config.claude_home, claude))
        pair.codex_cursor = _codex_start(engine.codex_path(pair))
        done = "linked; turns from now on are copied both ways"
    elif claude:
        pair, done = _codex_from_claude(engine, claude, name)
    else:
        pair, done = _claude_from_codex(engine, codex, name)
    engine.pairs[pair.name] = pair
    engine.save()
    engine.log.write(pair.name, "link", done)
    return pair, done


def _codex_from_claude(engine: Engine, claude: str, name: str | None) -> tuple[Pair, str]:
    engine.codex_gate()
    cpath = find_transcript(engine.config.claude_home, claude)
    start = _claude_start(cpath)
    lines = read_lines(cpath, 0, start.end)
    records = [ln.record for ln in lines]
    cwd = next((r["cwd"] for r in reversed(records) if r.get("cwd")), None) or os.getcwd()
    budget, outputs, kept = engine.config.copy_budget, engine.outputs, {}

    def build(thread_id: str) -> list[RolloutLine]:
        out = claude_to_codex(
            records,
            thread_id=thread_id,
            key=f"{claude}/{thread_id}",
            outputs=outputs,
            default_cwd=cwd,
        )
        turns, dropped = newest(_codex_turns(out), _model_size, budget)
        kept["turns"], kept["dropped"] = len(turns), dropped
        if not turns:
            raise LinkError("the Claude session has nothing to copy yet")
        lines = [ln for turn in turns for ln in turn]
        if dropped:
            note = (
                f"this thread continues a Claude Code session; its {dropped} earlier turn(s) "
                f"are not copied here. They are in {cpath}"
            )
            lines = (
                note_turn(
                    note, thread_id=thread_id, key=f"{claude}/{thread_id}", ms=lines[0].ms - 1
                )
                + lines
            )
        return lines

    codex, made = codex_threads.create_thread(
        engine.codex,
        cwd=cwd,
        build=build,
        journal=MemoryJournal(),
        source=engine.config.codex_source,
    )
    pair = Pair(
        name=_unique(engine, name or _claude_title(engine, claude)), claude=claude, codex=codex
    )
    pair.claude_cursor = start
    pair.codex_cursor = Cursor(made.end, prefix_hash(made.path, made.end), made.turns[-1])
    done = f"created Codex thread {codex} with {kept['turns']} turn(s) from Claude"
    if kept["dropped"]:
        done += f" ({kept['dropped']} older turn(s) left out: over the copy budget)"
    return pair, done


def _claude_from_codex(engine: Engine, codex: str, name: str | None) -> tuple[Pair, str]:
    xpath = engine.codex.rollout(codex)
    lines = read_lines(engine.codex_path(Pair(name="", claude="", codex=codex)))
    ends = turn_ends(lines)
    if not ends:
        raise LinkError("the Codex thread has no finished turn to copy yet")
    finished = [ln for ln in lines if ln.end <= ends[-1][1]]
    groups = _rollout_turns(finished)
    cwd = session_meta(xpath).get("cwd") or os.getcwd()
    title = _unique(engine, name or _codex_title(finished))
    version = claude_version(engine.config.claude_bin)
    budget, outputs, kept = engine.config.copy_budget, engine.outputs, {}

    def build(session_id: str) -> list[dict]:
        key = f"{session_id}/{codex}"
        per_turn = [codex_to_claude_steps(g, key=key, outputs=outputs) for g in groups]
        turns, dropped = newest([t for t in per_turn if t], _steps_size, budget)
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
            session_id=session_id,
            parent_uuid=None,
            parent_type=None,
            model="<synthetic>",
            version=version,
            cwd=cwd,
            lead_extra=extra,
        )

    claude, made = claude_sessions.create_session(
        engine.config.claude_home, cwd=cwd, build=build, journal=MemoryJournal(), title=title
    )
    pair = Pair(name=title, claude=claude, codex=codex)
    pair.claude_cursor = Cursor(made.end, prefix_hash(made.path, made.end), made.uuids[-1])
    pair.codex_cursor = Cursor(ends[-1][1], prefix_hash(xpath, ends[-1][1]), ends[-1][0])
    done = f"created Claude session {claude} with {kept['turns']} turn(s) from Codex"
    if kept["dropped"]:
        done += f" ({kept['dropped']} older turn(s) left out: over the copy budget)"
    return pair, done


# ---------------------------------------------------------------------------- the other verbs
def release(engine: Engine, pair: Pair) -> str:
    """Take a pair off hold, or unblock it. A side rewritten under duologue syncs on from where
    it stands now: the cursor moves to its anchor if that is still there, else to the end."""
    said = []
    if pair.hold:
        pair.hold = None
        said.append("taken off hold")
    if pair.blocked:
        pair.blocked = None
        said.append("unblocked")
        cpath, xpath = engine.paths(pair)
        if not _cursor_matches(cpath, pair.claude_cursor):
            pair.claude_cursor = _reanchor_claude(cpath, pair.claude_cursor)
            said.append("the Claude side syncs on from where it stands")
        if not _cursor_matches(xpath, pair.codex_cursor):
            pair.codex_cursor = _reanchor_codex(xpath, pair.codex_cursor)
            said.append("the Codex side syncs on from where it stands")
    pair.seen = []
    text = "; ".join(said) or "nothing to release"
    engine.save()
    engine.log.write(pair.name, "release", text)
    return text


def resolve(engine: Engine, pair: Pair, keep: str) -> str:
    """End a conflict by keeping one side's new turns; the other side's new turns are not
    copied (they stay in that app)."""
    if not pair.conflict:
        return "there is no conflict to resolve"
    cpath, xpath = engine.paths(pair)
    if keep == "claude":
        lines = read_lines(xpath, pair.codex_cursor.end)
        ends = turn_ends(lines)
        if ends:
            pair.codex_cursor = Cursor(ends[-1][1], prefix_hash(xpath, ends[-1][1]), ends[-1][0])
        text = "keeping Claude's new turns; Codex's new turns stay in Codex only"
    elif keep == "codex":
        pair.claude_cursor = _claude_start(cpath)
        text = "keeping Codex's new turns; Claude's new turns stay in Claude only"
    else:
        raise LinkError("keep claude or codex")
    pair.conflict = False
    pair.seen = []
    engine.save()
    engine.log.write(pair.name, "resolve", text)
    return text


def unlink(engine: Engine, pair: Pair, force: bool = False) -> str:
    if pair.pending and not force:
        raise LinkError(
            "a write to this pair is not confirmed yet; run duologue sync first, or unlink "
            "with --force"
        )
    engine.forget_pending(pair)
    del engine.pairs[pair.name]
    engine.save()
    engine.log.write(pair.name, "unlink", "unlinked; both conversations stay as they are")
    return "unlinked; both conversations stay as they are"


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
    raise LinkError(f"no pair {ref!r}; duologue status lists them")


# ------------------------------------------------------------------------------------ helpers
def newest(groups: Sequence[T], size: Callable[[T], int], budget: int) -> tuple[list[T], int]:
    """The newest groups whose sizes add up to at most ``budget`` (at least one), and how many
    older ones are left out."""
    kept: list[T] = []
    total = 0
    for g in reversed(groups):
        s = size(g)
        if kept and total + s > budget:
            break
        kept.append(g)
        total += s
    kept.reverse()
    return kept, len(groups) - len(kept)


def claude_version(claude_bin: str) -> str:
    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\d+\.\d+\.\d+", out)
    return match.group(0) if match else ""


def _codex_turns(lines: list[RolloutLine]) -> list[list[RolloutLine]]:
    turns: list[list[RolloutLine]] = []
    for ln in lines:
        if ln.type == "event_msg" and ln.payload.get("type") == "task_started":
            turns.append([])
        if turns:
            turns[-1].append(ln)
    return turns


def _rollout_turns(lines: list[Line]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for ln in lines:
        p = ln.record.get("payload")
        if (
            ln.record.get("type") == "event_msg"
            and isinstance(p, dict)
            and p.get("type") == "task_started"
        ):
            groups.append([])
        if groups:
            groups[-1].append(ln.record)
    return groups


def _model_size(turn: list[RolloutLine]) -> int:
    """What a turn costs the Codex model: its model-visible lines."""
    return sum(
        len(json.dumps(ln.payload, ensure_ascii=False)) for ln in turn if ln.type == "response_item"
    )


def _steps_size(steps: list[ClaudeStep]) -> int:
    return sum(len(json.dumps(s.blocks, ensure_ascii=False)) for s in steps)


def _claude_start(cpath: Path) -> Cursor:
    """Where syncing a linked Claude session starts: its end, or the start of a turn Claude is
    still working on (that turn is copied once it ends)."""
    lines = read_lines(cpath)
    end = lines[-1].end if lines else 0
    if not settled(lines):
        for i in range(len(lines) - 1, -1, -1):
            if is_user_input(lines[i].record):
                end = lines[i - 1].end if i else 0
                break
    anchor = next(
        (ln.record["uuid"] for ln in reversed(lines) if ln.end <= end and ln.record.get("uuid")),
        None,
    )
    return Cursor(end, prefix_hash(cpath, end), anchor)


def _codex_start(xpath: Path) -> Cursor:
    """Where syncing a linked Codex thread starts: after its last finished turn."""
    ends = turn_ends(read_lines(xpath))
    if not ends:
        return Cursor(0, hashlib.sha256(b"").hexdigest(), None)
    turn, end = ends[-1]
    return Cursor(end, prefix_hash(xpath, end), turn)


def _cursor_matches(path: Path, cur: Cursor) -> bool:
    return path.stat().st_size >= cur.end and prefix_hash(path, cur.end) == cur.digest


def _reanchor_claude(cpath: Path, cur: Cursor) -> Cursor:
    for ln in read_lines(cpath):
        if cur.anchor and ln.record.get("uuid") == cur.anchor:
            return Cursor(ln.end, prefix_hash(cpath, ln.end), cur.anchor)
    return _claude_start(cpath)


def _reanchor_codex(xpath: Path, cur: Cursor) -> Cursor:
    for turn, end in reversed(turn_ends(read_lines(xpath))):
        if cur.anchor and turn == cur.anchor:
            return Cursor(end, prefix_hash(xpath, end), turn)
    return _codex_start(xpath)


def _unique(engine: Engine, name: str) -> str:
    name = " ".join(name.split())[:80] or "conversation"
    if name not in engine.pairs:
        return name
    n = 2
    while f"{name} ({n})" in engine.pairs:
        n += 1
    return f"{name} ({n})"


def _claude_title(engine: Engine, claude: str) -> str:
    title, first = None, None
    for ln in read_lines(find_transcript(engine.config.claude_home, claude)):
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
