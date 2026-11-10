"""Pairing conversations by themselves.

Each pass (``duologue sync``, which the background service runs every 30 seconds) looks for
Claude Code sessions and Codex threads that changed within ``auto_days`` and are not paired yet,
newest first, and gives each its other side, as ``duologue link`` does. From then on the pair
syncs both ways like any other.

Left out:

- conversations duologue made itself (the other side of a pair);
- scripted runs: ``claude -p`` and the Agent SDK, ``codex exec``, unless ``auto_scripted``;
- Codex subagent threads, and Claude sessions that continue in another session;
- archived Codex threads, and ones Codex compressed (it writes them out again when they are
  opened, and they are found then);
- conversations whose folder is gone, or with no finished turn yet;
- the two sides of a pair the user unlinked: duologue does not pair them again by itself.

A Claude session that Codex's own importer already copied is paired with that thread, so it
does not get a second copy, as long as the session still starts with what Codex copied (else it
gets a thread of its own, and Codex's copy is left as it is).

What was looked at and left out is remembered with the file's size and time, so an unchanged
file is not read again; it is looked at again once it changes.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from duologue.claude.transcript import PROJECTS, find_transcript, is_message, is_typed
from duologue.codex.rollout import (
    SESSIONS,
    is_archived,
    is_compressed,
    payload,
    session_meta,
    turn_ends,
)
from duologue.engine import Engine
from duologue.ids import UUID
from duologue.journal import Wait
from duologue.jsonl import iter_lines, read_lines
from duologue.pairs import write_atomic
from duologue.translate.common import CODEX_IMPORT_TURN, ORIGIN

PER_PASS = 10  # conversations paired in one pass; the rest wait for the next
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


@dataclass
class AutoState:
    """``<home>/auto.json``: the ids not to pair by itself, and the files found not to pair as
    they stood."""

    path: Path
    ignored: set[str] = field(default_factory=set)
    passed: dict[str, list] = field(default_factory=dict)  # path -> [size, mtime_ns, why]
    settings: dict = field(default_factory=dict)  # the ones the reasons in ``passed`` rest on
    changed: bool = False

    @classmethod
    def load(cls, path: Path) -> AutoState:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except ValueError:
            return cls(path)  # bookkeeping only: start again rather than stop syncing
        if not isinstance(data, dict):
            return cls(path)
        ignored, passed, settings = data.get("ignored"), data.get("passed"), data.get("settings")
        return cls(
            path,
            {x for x in ignored if isinstance(x, str)} if isinstance(ignored, list) else set(),
            dict(passed) if isinstance(passed, dict) else {},
            dict(settings) if isinstance(settings, dict) else {},
        )

    def pass_over(self, f: Found, why: str) -> bool:
        """Remember that ``f`` was left out as it stands, and why; whether that is news (the
        first time, or another reason than last time)."""
        old = self.passed.get(str(f.path))
        self.passed[str(f.path)] = [*f.stamp, why]
        self.changed = True
        return not (isinstance(old, list) and old[2:] == [why])

    def save(self) -> None:
        data = {"ignored": sorted(self.ignored), "passed": self.passed, "settings": self.settings}
        write_atomic(self.path, (json.dumps(data, indent=1) + "\n").encode())

    def ignore(self, *ids: str) -> None:
        self.ignored.update(ids)
        self.save()

    def unignore(self, *ids: str) -> None:
        if self.ignored & set(ids):
            self.ignored -= set(ids)
            self.save()


def state_file(engine: Engine) -> Path:
    return engine.config.home / "auto.json"


# --------------------------------------------------------------------------------- finding
def found(engine: Engine, state: AutoState, now: float | None = None) -> list[Found]:
    """The conversations that changed within ``auto_days`` and are not paired, ignored, or
    passed over as they stand, newest first."""
    cutoff = (time.time() if now is None else now) - engine.config.auto_days * DAY
    taken = {p.claude for p in engine.pairs.values()} | {p.codex for p in engine.pairs.values()}
    out: dict[tuple[str, str], Found] = {}

    def consider(side: str, ident: str, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        stamp = (st.st_size, st.st_mtime_ns)
        if st.st_mtime < cutoff or ident in taken or ident in state.ignored:
            return
        if (state.passed.get(str(path)) or [])[:2] == list(stamp):
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
    """Forget passed-over files that are gone or too old to be found again."""
    for name in list(state.passed):
        try:
            if os.stat(name).st_mtime >= cutoff:
                continue
        except OSError:
            pass
        del state.passed[name]
        state.changed = True


# ------------------------------------------------------------------------------- deciding
def claude_reason(engine: Engine, path: Path) -> str | None:
    """Why the Claude session in ``path`` is not paired by itself, or None when it is."""
    typed = scripted = False
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
        if isinstance(entry, str) and entry.startswith("sdk"):
            scripted = True
        typed = typed or is_typed(r)
    if scripted and not engine.config.auto_scripted:
        return "a script ran it"
    if not typed:
        return "nothing was typed in it yet"
    if not cwd or not os.path.isdir(cwd):
        return "its folder is gone"
    return None


def codex_reason(engine: Engine, path: Path, thread_id: str) -> str | None:
    """Why the Codex thread in ``path`` is not paired by itself, or None when it is."""
    if is_compressed(path):
        return "Codex compressed it"
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
    return None


# ------------------------------------------------------------------------- Codex's importer
@dataclass(frozen=True)
class Imported:
    claude: str  # the Claude session's id
    codex: str  # the thread Codex's importer made of it
    sha256: str  # the Claude transcript's sha256 when Codex last copied it


def codex_imports(engine: Engine) -> list[Imported]:
    """What Codex's own importer copied from Claude Code sessions, oldest first."""
    try:
        data = json.loads((engine.config.codex_home / CODEX_LEDGER).read_text(encoding="utf-8"))
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


def import_of(engine: Engine, f: Found, imports: list[Imported]) -> Imported | None:
    """The import pairing the conversation ``f`` with its other side, when Codex's importer made
    one and both sides are still there and free."""
    taken = {p.claude for p in engine.pairs.values()} | {p.codex for p in engine.pairs.values()}
    for imp in reversed(imports):  # the newest import wins
        if (f.side == "claude" and imp.claude != f.id) or (f.side == "codex" and imp.codex != f.id):
            continue
        if imp.claude in taken or imp.codex in taken:
            continue
        if find_transcript(engine.config.claude_home, imp.claude) is None:
            continue
        xpath = engine.codex.rollout(imp.codex)
        if xpath is None or is_archived(engine.config.codex_home, xpath):
            continue
        return imp
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
    """Pair the conversations found, at most :data:`PER_PASS` of them; what was done, one line
    each. A conversation that cannot be paired now is tried again on a later pass."""
    from duologue import linking  # linking imports the engine, as this module does

    state = AutoState.load(state_file(engine))
    now = time.time() if now is None else now
    settings = {"auto_scripted": engine.config.auto_scripted}
    if state.settings != settings:  # what was passed over may be paired now
        state.passed, state.settings, state.changed = {}, settings, True
    prune(state, now - engine.config.auto_days * DAY)
    imports = codex_imports(engine)
    said: list[str] = []
    try:
        candidates = found(engine, state, now)
    except OSError as e:  # a folder that cannot be read: the pairs still sync
        engine.log.write(None, "error", f"looking for new conversations: {e}")
        return said
    for f in candidates:
        if len(said) >= PER_PASS:
            break
        if any(f.id in (p.claude, p.codex) for p in engine.pairs.values()):
            continue  # paired earlier in this pass, as the other side of something found
        try:
            imp = import_of(engine, f, imports)
            if imp is not None:
                try:
                    pair, done = linking.link_import(engine, imp.claude, imp.codex, imp.sha256)
                except linking.NotImported:
                    imp = None  # Codex's copy is stale: as if there were none
            if imp is None:
                reason = why_not(engine, f)
                if reason:
                    state.pass_over(f, reason)
                    continue
                pair, done = linking.link(
                    engine,
                    claude=f.id if f.side == "claude" else None,
                    codex=f.id if f.side == "codex" else None,
                    name=None,
                    cwd=os.getcwd(),
                )
        except Wait:
            continue  # the thread is open, a turn is running, Codex is untested: next pass
        except linking.LinkError as e:
            if isinstance(e.__cause__, Wait):
                continue
            if state.pass_over(f, str(e)):
                engine.log.write(None, "auto", f"{SIDE[f.side]} {f.id} not paired: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - one conversation must not stop the others
            text = f"{type(e).__name__}: {e}"
            if state.pass_over(f, text):
                engine.log.write(None, "error", f"pairing {SIDE[f.side]} {f.id}: {text}")
            continue
        said.append(f'"{pair.name}": {done}')
    if state.changed:
        state.save()
    return said


def why_not(engine: Engine, f: Found) -> str | None:
    """Why the conversation ``f`` is not paired by itself, or None when it is."""
    if f.side == "claude":
        return claude_reason(engine, f.path)
    reason = codex_reason(engine, f.path, f.id)
    if reason is None and made_by_codex_import(f.path):
        # Its Claude session is gone, paired already or unlinked: a copy would be a duplicate.
        reason = "Codex's importer made it from a Claude session that is not free to pair"
    return reason
