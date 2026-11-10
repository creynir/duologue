"""Pairing conversations by themselves.

Each pass (``duologue sync``, which the background service runs every 30 seconds) looks for
Claude Code sessions and Codex threads that changed within ``auto_days`` and are not paired yet,
newest first, and gives each its other side, as ``duologue link`` does: at most
:data:`PER_PASS` tries a pass, the rest on the next. From then on the pair syncs both ways like
any other.

Left out:

- conversations duologue made itself (the other side of a pair);
- scripted runs: ``claude -p`` and the Agent SDK, ``codex exec``, unless ``auto_scripted``;
- Codex subagent threads, threads rewound in Codex, and Claude sessions that continue in
  another session;
- archived Codex threads, and ones Codex compressed while idle (it writes them out again when
  they are opened, and they are found then);
- conversations whose folder is gone, or with nothing typed or no finished turn yet;
- the two sides of a pair the user unlinked: duologue does not pair them again by itself.

A Claude session that Codex's own importer already copied is paired with that thread, so it
does not get a second copy, when the session still starts with what Codex copied. When it does
not, the two are paired from now on if the user carried on in that thread in Codex; otherwise
the session gets a thread of its own, and Codex's copy is left as it is.

What was looked at and left out is remembered with the file's size and time, so an unchanged
file is not read again; it is looked at again once it changes. A try that failed for another
reason (Codex did not answer, say) is tried again after :data:`RETRY_AFTER` seconds, and logged
once. ``duologue status`` sums all of it up.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from duologue import linking
from duologue.claude.transcript import PROJECTS, find_transcript, is_message
from duologue.codex.rollout import (
    SESSIONS,
    is_archived,
    is_compressed,
    payload,
    session_meta,
    turn_ends,
)
from duologue.config import Config
from duologue.engine import Engine
from duologue.ids import UUID
from duologue.journal import Wait
from duologue.jsonl import iter_lines, read_lines
from duologue.pairs import AutoState, AutoStateError, auto_state_file
from duologue.translate.claude_to_codex import is_user_input
from duologue.translate.common import CODEX_IMPORT_TURN, ORIGIN

PER_PASS = 10  # tries in one pass; the rest wait for the next
RETRY_AFTER = 600  # seconds before a try that failed is made again
DAY = 86_400
CODEX_LEDGER = "external_agent_session_imports.json"  # what Codex's importer copied
ROLLOUT_NAME = re.compile(rf"rollout-.+?-({UUID.pattern})(?:_[^/]*)?\.jsonl(?:\.zst)?")
SCRIPTED_CODEX = ("exec", "mcp")
SIDE = {"claude": "Claude session", "codex": "Codex thread"}


@dataclass(frozen=True)
class Found:
    side: str  # "claude" or "codex"
    id: str
    path: Path
    stamp: tuple[int, int]  # size and mtime in ns, when found
    mtime: float


# --------------------------------------------------------------------------------- finding
def found(engine: Engine, state: AutoState, now: float) -> list[Found]:
    """The conversations that changed within ``auto_days`` and are not paired, ignored, passed
    over as they stand or waiting to be tried again, newest first."""
    cutoff = now - engine.config.auto_days * DAY
    taken = {p.claude for p in engine.pairs.values()} | {p.codex for p in engine.pairs.values()}
    out: dict[tuple[str, str], Found] = {}

    def consider(side: str, ident: str, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode):  # a pipe or a device would hang the pass
            return
        stamp = (st.st_size, st.st_mtime_ns)
        if ident in taken:  # paired, by hand or by an earlier pass: nothing to remember
            for memo in (state.passed, state.failed):
                if memo.pop(str(path), None) is not None:
                    state.changed = True
            return
        if st.st_mtime < cutoff or ident in state.ignored:
            return
        if (state.passed.get(str(path)) or [])[:2] == list(stamp):
            return
        tried = state.failed.get(str(path))
        if tried and now - tried[0] < RETRY_AFTER:
            return
        old = out.get((side, ident))
        if old is None or old.mtime < st.st_mtime:
            out[(side, ident)] = Found(side, ident, path, stamp, st.st_mtime)

    projects = engine.config.claude_home / PROJECTS
    if projects.is_dir():
        for path in projects.glob("*/*.jsonl"):
            if UUID.fullmatch(path.stem):
                consider("claude", path.stem, path)
    sessions = engine.config.codex_home / SESSIONS
    if sessions.is_dir():
        for path in sessions.rglob("rollout-*.jsonl*"):
            m = ROLLOUT_NAME.fullmatch(path.name)
            if m:
                consider("codex", m.group(1), path)
    return sorted(out.values(), key=lambda f: f.mtime, reverse=True)


def prune(state: AutoState, cutoff: float) -> None:
    """Forget files that are gone or too old to be found again."""
    for memo in (state.passed, state.failed):
        for name in list(memo):
            try:
                if os.stat(name).st_mtime >= cutoff:
                    continue
            except OSError:
                pass
            del memo[name]
            state.changed = True


# ------------------------------------------------------------------------------- deciding
def claude_reason(config: Config, path: Path) -> str | None:
    """Why the Claude session in ``path`` is not paired by itself, or None when it is."""
    typed = False
    cwd = None
    first = True
    for ln in iter_lines(path):
        r = ln.record
        if r.get("type") == "continued-in":
            return "it continues in another session"
        if is_message(r):
            if first and r.get(ORIGIN):
                return "duologue made it"
            first = False
        if cwd is None and isinstance(r.get("cwd"), str) and r["cwd"]:
            cwd = r["cwd"]
        entry = r.get("entrypoint")
        if isinstance(entry, str) and entry.startswith("sdk") and not config.auto_scripted:
            return "a script ran it"
        typed = typed or is_user_input(r)
    if not typed:
        return "nothing was typed in it yet"
    if not cwd or not os.path.isdir(cwd):
        return "its folder is gone"
    return None


def codex_reason(engine: Engine, path: Path, thread_id: str) -> str | None:
    """Why the Codex thread in ``path`` is not paired by itself, or None when it is."""
    if is_compressed(path):
        return "Codex compressed it while it was idle"
    current = engine.codex.rollout(thread_id)
    if current is None or current != path:
        return "it continues in another file"
    meta = session_meta(path)
    if meta.get("id") != thread_id:
        return "its first line does not name it"
    if meta.get("originator") == "duologue":
        return "duologue made it"
    source = meta.get("source")
    if isinstance(source, dict):
        return "a subagent thread"
    if source in SCRIPTED_CODEX and not engine.config.auto_scripted:
        return "a script ran it"
    if meta.get("history_base"):
        return "it was rewound in Codex"
    cwd = meta.get("cwd")
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return "its folder is gone"
    if not turn_ends(read_lines(path)):
        return "no turn has finished in it yet"
    if made_by_codex_import(path):
        # Its Claude session is gone, paired already or unlinked: a copy would be a duplicate.
        return "Codex's importer made it from a Claude session that is not free to pair"
    return None


# ------------------------------------------------------------------------- Codex's importer
@dataclass(frozen=True)
class Imported:
    claude: str  # the Claude session's id
    codex: str  # the thread Codex's importer made of it
    sha256: str  # the Claude transcript's sha256 when Codex last copied it


def codex_imports(config: Config) -> list[Imported]:
    """What Codex's own importer copied from Claude Code sessions, oldest first."""
    path = config.codex_home / CODEX_LEDGER
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    out = []
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        source, thread = r.get("source_path"), r.get("imported_thread_id")
        sha = r.get("content_sha256")
        if not (isinstance(source, str) and isinstance(thread, str) and isinstance(sha, str)):
            continue
        stem = Path(source).name.removesuffix(".jsonl")
        if UUID.fullmatch(stem) and UUID.fullmatch(thread):
            out.append(Imported(stem, thread, sha))
    return out


def import_of(engine: Engine, state: AutoState, f: Found, imports: list[Imported]):
    """The import pairing the conversation ``f`` with its other side, when Codex's importer made
    one and both sides are still there, free and not unlinked; with the Claude transcript."""
    taken = {p.claude for p in engine.pairs.values()} | {p.codex for p in engine.pairs.values()}
    for imp in reversed(imports):  # the newest import wins
        if (f.side == "claude" and imp.claude != f.id) or (f.side == "codex" and imp.codex != f.id):
            continue
        if {imp.claude, imp.codex} & (taken | state.ignored):
            continue
        cpath = find_transcript(engine.config.claude_home, imp.claude)
        if cpath is None or not cpath.is_file():
            continue
        xpath = engine.codex.rollout(imp.codex)
        if xpath is None or is_archived(engine.config.codex_home, xpath):
            continue
        if not is_compressed(xpath) and session_meta(xpath).get("history_base"):
            continue  # rewound in Codex: the import's turns are in another file
        return imp, cpath
    return None


def made_by_codex_import(path: Path) -> bool:
    """Whether the thread's first turn is one Codex's importer wrote."""
    for ln in iter_lines(path):
        p = payload(ln.record)
        if ln.record.get("type") == "event_msg" and p.get("type") == "task_started":
            return str(p.get("turn_id") or "").startswith(CODEX_IMPORT_TURN)
    return False


# ---------------------------------------------------------------------------------- pairing
def link_new(engine: Engine, now: float | None = None) -> list[str]:
    """Pair the conversations found, at most :data:`PER_PASS` tries; what was done, one line
    each. A conversation that cannot be paired now is tried again on a later pass. Raises
    :class:`AutoStateError` when ``auto.json`` cannot be read."""
    state = AutoState.load(auto_state_file(engine.config))
    now = time.time() if now is None else now
    settings = {"auto_scripted": engine.config.auto_scripted}
    if state.settings != settings:  # what was passed over may be paired now
        state.passed, state.settings, state.changed = {}, settings, True
    prune(state, now - engine.config.auto_days * DAY)
    waiting, state.waiting = state.waiting, {}
    try:
        engine.codex_gate()
        gate = None
    except Wait as e:  # no thread can be made in Codex: Claude sessions wait, unread
        gate = str(e)
    imports = codex_imports(engine.config)
    said: list[str] = []
    tries = 0

    def pair_one(f: Found):
        """Pair ``f``: the pair and what was done, or a reason to pass it over (a str)."""
        nonlocal tries
        imported = import_of(engine, state, f, imports)
        if imported is not None:
            imp, cpath = imported
            reason = claude_reason(engine.config, cpath)
            if reason:
                return f"its Claude session: {reason}" if f.side == "codex" else reason
            tries += 1
            try:
                return linking.link_import(engine, imp.claude, imp.codex, imp.sha256)
            except linking.NotImported as e:
                if e.continued:  # the user carried on in Codex's copy: they sync from now on
                    pair, done = linking.link(
                        engine, claude=imp.claude, codex=imp.codex, name=None, cwd=os.getcwd()
                    )
                    return pair, f"{e}; {done}"
                if f.side == "codex":
                    return "Codex's copy of a Claude session that changed since"
                # The Claude session gets a thread of its own, as if there were no import.
        if f.side == "claude" and gate:
            raise Wait(gate)
        reason = (
            claude_reason(engine.config, f.path)
            if f.side == "claude"
            else codex_reason(engine, f.path, f.id)
        )
        if reason:
            return reason
        tries += 1
        return linking.link(
            engine,
            claude=f.id if f.side == "claude" else None,
            codex=f.id if f.side == "codex" else None,
            name=None,
            cwd=os.getcwd(),
        )

    try:
        candidates = found(engine, state, now)
    except OSError as e:  # a folder that cannot be read: the pairs still sync
        engine.log.write(None, "error", f"looking for new conversations: {e}")
        candidates = []
    for f in candidates:
        if tries >= PER_PASS:
            break
        if any(f.id in (p.claude, p.codex) for p in engine.pairs.values()):
            continue  # paired earlier in this pass, as the other side of something found
        try:
            result = pair_one(f)
        except Wait as e:
            state.waiting[f.id] = str(e)  # the thread is open, Codex is untested: next pass
            continue
        except linking.LinkError as e:
            if isinstance(e.__cause__, Wait):
                state.waiting[f.id] = str(e)
            elif e.__cause__ is None:  # about the conversation as it stands
                state.pass_over(str(f.path), f.stamp, str(e))
            elif state.fail(str(f.path), str(e), now):
                engine.log.write(None, "auto", f"{SIDE[f.side]} {f.id} not paired yet: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - one conversation must not stop the others
            text = f"{type(e).__name__}: {e}"
            if state.fail(str(f.path), text, now):
                engine.log.write(None, "error", f"pairing {SIDE[f.side]} {f.id}: {text}")
            continue
        if isinstance(result, str):
            state.pass_over(str(f.path), f.stamp, result)
            continue
        pair, done = result
        if state.failed.pop(str(f.path), None):
            state.changed = True
        said.append(f'"{pair.name}": {done}')
    state.changed |= state.waiting != waiting
    if state.changed:
        state.save()
    return said


def summary(config: Config) -> list[str]:
    """What the last passes found and could not pair, for ``duologue status``."""
    try:
        state = AutoState.load(auto_state_file(config))
    except AutoStateError as e:
        return [f"nothing is paired by itself: {e}; fix it, or remove it to forget the unlinks"]
    out = []
    if state.waiting:
        reasons = Counter(state.waiting.values())
        out.append(
            f"waiting to be paired: {len(state.waiting)} ("
            + "; ".join(f"{n} {why}" for why, n in reasons.most_common())
            + ")"
        )
    if state.failed:
        out.append(f"not paired yet, tried again later: {len(state.failed)} (see duologue log)")
    left = Counter(entry[2] for entry in state.passed.values() if len(entry) > 2)
    if left:
        out.append(
            f"left out: {sum(left.values())} ("
            + "; ".join(f"{n} {why}" for why, n in left.most_common())
            + ")"
        )
    if state.ignored:
        out.append(f"unlinked, not paired again by itself: {len(state.ignored)} conversation(s)")
    return out
