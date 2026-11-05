"""Syncing a pair: what each side gained since its cursor, copied to the other side.

One pass over a pair:

1. A held, blocked or conflicted pair is left alone.
2. An append an earlier pass did not confirm (duologue stopped, or Codex was busy before it
   indexed) is settled first: confirmed, or found missing and dropped.
3. Each cursor is checked against its file. A Claude transcript changed before its cursor blocks
   the pair; for a Codex thread, which Codex rewrites when it converts one, the cursor's turn is
   found again, and only if it is gone does the pair block.
4. New content on both sides is a conflict: the user wrote in both apps. Nothing is copied until
   they keep one side (``duologue resolve``).
5. New Claude content goes to Codex once Claude's turn has ended and the transcript has been
   quiet a moment. Codex's finished turns go to Claude while no Claude Code process has the
   session open.

Every write records its intent first (a :class:`~duologue.pairs.Pending` and a journal file), and
the cursors move only after the write is confirmed.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from duologue.claude import sessions as claude_sessions
from duologue.claude.live import live_pid
from duologue.claude.transcript import find_transcript, is_typed, settled, tail
from duologue.codex import lock
from duologue.codex import threads as codex_threads
from duologue.codex.rollout import is_compressed, session_meta, turn_ends
from duologue.codex.rpc import AppServerError, daemon_running
from duologue.codex.threads import Codex
from duologue.config import Config
from duologue.gate import CodexFormat, cached_format
from duologue.journal import Blocked, Intent, Wait, prefix_hash, tail_state
from duologue.jsonl import Line, iter_lines, read_lines
from duologue.pairs import ActivityLog, Cursor, Pair, Pending, Store, now_iso, write_atomic
from duologue.translate.claude_to_codex import claude_to_codex, has_content
from duologue.translate.codex_to_claude import ClaudeStep, claude_records, codex_to_claude_steps
from duologue.translate.outputs import OutputStore

CONFLICT = (
    "you wrote in both Claude and Codex since the last sync, so nothing was copied; keep one "
    'side\'s new turns with: duologue resolve "{name}" --keep claude (or --keep codex)'
)
KNOWN_RECORD_TYPES = ("user", "assistant")  # the only chain records that carry a message
TRANSIENT = (AppServerError, OSError, TimeoutError, sqlite3.Error, subprocess.SubprocessError)


@dataclass(frozen=True)
class Outcome:
    state: str  # synced, idle, waiting, held, blocked, conflict or error
    text: str


class Engine:
    def __init__(self, config: Config, pairs: dict[str, Pair], store: Store, log: ActivityLog):
        self.config = config
        self.pairs = pairs
        self.store = store
        self.log = log
        self.codex = Codex(config.codex_home, config.codex_bin)
        self.outputs = OutputStore(config.outputs_dir)
        self._format: CodexFormat | None = None

    # -------------------------------------------------------------------------------- a pass
    def sync(self, pair: Pair, force: bool = False) -> Outcome:
        """One pass over ``pair``; saves the state and reports what happened. Nothing that goes
        wrong with one pair stops the pass over the others."""
        if pair.hold:
            return Outcome("held", f"on hold: {pair.hold}")
        if pair.blocked:
            return Outcome("blocked", pair.blocked)
        if pair.conflict:
            return Outcome("conflict", CONFLICT.format(name=pair.name))
        try:
            outcome = self._sync(pair, force)
        except Wait as e:
            outcome = Outcome("waiting", str(e))
        except Blocked as e:
            self.block(pair, str(e))
            outcome = Outcome("blocked", str(e))
        except Exception as e:  # noqa: BLE001 - one pair's failure must not end the pass
            outcome = Outcome("error", f"{type(e).__name__}: {e}")
            self.log.write(pair.name, "error", outcome.text)
        if outcome.state != "idle":
            pair.seen = []
        pair.status = f"{outcome.state}: {outcome.text}"
        self.save()
        return outcome

    def _sync(self, pair: Pair, force: bool) -> Outcome:
        if pair.pending:
            self.settle(pair)
        cpath, xpath = self.paths(pair)
        seen = [_stamp(cpath), _stamp(xpath)]
        if not force and pair.seen == seen:
            return Outcome("idle", "up to date")
        ccur = self.claude_cursor(pair, cpath, seen[0])
        xcur = self.codex_cursor(pair, xpath, seen[1])
        csize, xsize = seen[0][1], seen[1][1]  # what this pass reads; a write expects them
        clines = read_lines(cpath, ccur.end, csize)
        xall = read_lines(xpath, xcur.end, xsize)
        ends = turn_ends(xall)
        xlines = [ln for ln in xall if ln.end <= ends[-1][1]] if ends else []
        _check_record_types(clines)
        if has_content(ln.record for ln in clines):
            if self.codex_steps(pair, xlines):
                pair.conflict = True
                text = CONFLICT.format(name=pair.name)
                self.log.write(pair.name, "conflict", text)
                self.notify(f'duologue: "{_printable(pair.name)}": {text}')
                return Outcome("conflict", text)
            return self.to_codex(pair, cpath, clines, xsize)
        if xlines:
            live = self.claude_live(pair)
            if live() is not None:  # before translating: a waiting pass should cost nothing
                raise Wait(
                    "the Claude session is open; Codex's turns are copied to it once it closes"
                )
            steps = self.codex_steps(pair, xlines)
            if steps:
                return self.to_claude(pair, cpath, xlines, steps, csize, live)
        pair.seen = seen
        return Outcome("idle", "up to date")

    # ---------------------------------------------------------------------------- directions
    def to_codex(self, pair: Pair, cpath: Path, clines: list[Line], xsize: int) -> Outcome:
        if not settled(clines) and not self.claude_stopped(pair, cpath):
            raise Wait("Claude is still working on its turn")
        if time.time() - cpath.stat().st_mtime < self.config.quiet_seconds:
            raise Wait("Claude's turn just ended; it is copied once the session is quiet")
        if lock.is_held(self.codex.home, pair.codex) and not daemon_running(self.codex.home):
            raise Wait("the thread is open in Codex")  # checked first: it is the cheap check
        self.codex_gate()
        cwd = next((ln.record["cwd"] for ln in reversed(clines) if ln.record.get("cwd")), "")
        cwd = cwd or session_meta(self.codex_path(pair)).get("cwd", "")
        lines = claude_to_codex(
            [ln.record for ln in clines],
            thread_id=pair.codex,
            key=pair.key,
            outputs=self.outputs,
            default_cwd=cwd,
        )
        after_claude = {
            "end": clines[-1].end,
            "anchor": _last_uuid(clines) or pair.claude_cursor.anchor,
        }
        journal = PairJournal(self, pair, claude=after_claude, codex=None)
        done = codex_threads.append_turns(
            self.codex, pair.codex, lines, journal=journal, expect_size=xsize
        )
        self.confirm(pair, after_claude, {"end": done.end, "anchor": done.turns[-1]})
        pair.copied["to_codex"] += len(done.turns)
        text = f"copied {len(done.turns)} Claude turn(s) to Codex"
        self.log.write(pair.name, "to_codex", text if done.written else text + " (already there)")
        return Outcome("synced", text)

    def to_claude(
        self,
        pair: Pair,
        cpath: Path,
        xlines: list[Line],
        steps: list[ClaudeStep],
        csize: int,
        live,
    ) -> Outcome:
        t = tail(iter_lines(cpath))
        records = claude_records(
            steps,
            session_id=pair.claude,
            parent_uuid=t.parent_uuid,
            parent_type=t.parent_type,
            model=t.model,
            version=t.version,
            cwd=t.cwd or session_meta(self.codex_path(pair)).get("cwd", ""),
            git_branch=t.git_branch,
            dangling=t.dangling,
            source_assistant=t.last_assistant,
        )
        turns = [turn for turn, _ in turn_ends(xlines)]
        after_codex = {"end": xlines[-1].end, "anchor": turns[-1] if turns else None}
        journal = PairJournal(self, pair, claude=None, codex=after_codex)
        done = claude_sessions.append_records(
            cpath, records, journal=journal, live=live, expect_size=csize
        )
        self.confirm(pair, {"end": done.end, "anchor": done.uuids[-1]}, after_codex)
        pair.copied["to_claude"] += len(turns)
        text = f"copied {len(turns)} Codex turn(s) to Claude"
        self.log.write(pair.name, "to_claude", text if done.written else text + " (already there)")
        return Outcome("synced", text)

    # ------------------------------------------------------------------------------- cursors
    def claude_cursor(self, pair: Pair, cpath: Path, stamp: list | None = None) -> Cursor:
        cur = pair.claude_cursor
        if self._still_matches("claude", pair, cpath, cur, stamp):
            return cur
        raise Blocked(
            "the Claude transcript changed before the point already synced; check it, then "
            "run duologue release to sync on from where it stands"
        )

    def codex_cursor(self, pair: Pair, xpath: Path, stamp: list | None = None) -> Cursor:
        cur = pair.codex_cursor
        if self._still_matches("codex", pair, xpath, cur, stamp):
            return cur
        if cur.anchor:  # Codex rewrites a whole thread when it converts it: find the turn again
            hits = [end for turn, end in turn_ends(iter_lines(xpath)) if turn == cur.anchor]
            if hits:
                found = Cursor(hits[-1], prefix_hash(xpath, hits[-1]), cur.anchor)
                pair.codex_cursor = found
                self.log.write(
                    pair.name,
                    "reanchor",
                    f"Codex rewrote the thread; synced point found again after turn {cur.anchor}",
                )
                return found
        raise Blocked(
            "the Codex thread changed before the point already synced; check it, then run "
            "duologue release to sync on from where it stands"
        )

    def _still_matches(
        self, side: str, pair: Pair, path: Path, cur: Cursor, stamp: list | None
    ) -> bool:
        """Whether the file still holds what the cursor hashed. A side whose file is unchanged
        since its cursor last matched is not hashed again."""
        if stamp is not None and pair.checked.get(side) == stamp:
            return True
        if not _matches(path, cur):
            return False
        if stamp is not None:
            pair.checked[side] = stamp
        return True

    def confirm(self, pair: Pair, claude: dict | None, codex: dict | None) -> None:
        """Move the cursors past a confirmed write, forget its intent, and save."""
        cpath, xpath = self.paths(pair)
        if claude:
            pair.claude_cursor = Cursor(
                claude["end"], prefix_hash(cpath, claude["end"]), claude["anchor"]
            )
        if codex:
            pair.codex_cursor = Cursor(
                codex["end"], prefix_hash(xpath, codex["end"]), codex["anchor"]
            )
        pair.checked = {}
        pair.last_sync = now_iso()
        self.forget_pending(pair)

    def settle(self, pair: Pair) -> None:
        """Settle an append an earlier pass did not confirm."""
        p = pair.pending
        assert p is not None
        try:
            data = self.journal_file(pair).read_bytes()
        except FileNotFoundError:
            data = None
        if data is None or hashlib.sha256(data).hexdigest() != p.sha256:
            # Its bytes are gone (duologue stopped just as it finished): the ids tell.
            if self._ids_present(pair, p):
                self.confirm(pair, p.claude, p.codex)
                return
            raise Blocked("the record of an interrupted write is missing or damaged")
        path = Path(p.path)
        if p.side == "claude":  # EnterWorktree moves a transcript: follow it
            path = find_transcript(self.config.claude_home, pair.claude) or path
        intent = Intent(p.side, path, p.base, data, tuple(p.ids), p.before)
        if p.side == "codex":
            done = codex_threads.recover(self.codex, pair.codex, intent)
        else:
            done = claude_sessions.recover(path, intent)
        if done:
            self.confirm(pair, p.claude, p.codex)
            self.log.write(pair.name, "recovered", f"an interrupted write to {p.side} is complete")
            return
        self.forget_pending(pair)
        self.log.write(pair.name, "recovered", f"an interrupted write to {p.side} was undone")
        if p.base == 0:  # a side `link` was creating: nothing of it was ever written
            raise Blocked(
                f"creating the {p.side.capitalize()} side was interrupted before anything was "
                "written; unlink this pair and link again"
            )

    def accept_pending(self, pair: Pair) -> None:
        """A person checked an interrupted write and releases the pair: take each side as it is
        now. A cursor the write would have reached is kept when its file still reaches it."""
        p = pair.pending
        if p is None:
            return
        cpath, xpath = self.paths(pair)
        landed = self._landed(pair, p, cpath if p.side == "claude" else xpath)
        if landed and p.side == "codex":
            ids, end = tuple(p.ids), p.base + p.length
            if not self.codex.index(pair.codex).holds(ids, end):
                # In the file but not in Codex's index, so not shown: take the lines out (only
                # when Codex indexed none of them and nothing follows), then copy them again.
                data = self.journal_file(pair).read_bytes()
                intent = Intent("codex", xpath, p.base, data, ids, p.before)
                if not codex_threads.take_out(self.codex, pair.codex, intent):
                    raise Blocked(
                        "the Codex thread holds lines duologue wrote that Codex has not indexed, "
                        "and something follows them; check the thread in Codex"
                    )
                landed = False
        if landed:
            # The journal proves the write is there: both cursors move past it.
            for side, target, path in (("claude", p.claude, cpath), ("codex", p.codex, xpath)):
                if target and target["end"] <= path.stat().st_size:
                    cur = Cursor(target["end"], prefix_hash(path, target["end"]), target["anchor"])
                    setattr(pair, f"{side}_cursor", cur)
            said = "an interrupted write is there; it counts as copied"
        else:
            # Not proven: the cursors stay, so what it was copying is copied again.
            said = "an interrupted write could not be confirmed; it is copied again"
        self.log.write(pair.name, "release", said)
        pair.checked = {}
        self.forget_pending(pair)

    def forget_pending(self, pair: Pair) -> None:
        """Drop the intent: save the state first, then remove its bytes, so a stop between the
        two never leaves a record pointing at bytes that are gone."""
        pair.pending = None
        self.save()
        self.journal_file(pair).unlink(missing_ok=True)

    def _landed(self, pair: Pair, p: Pending, path: Path) -> bool:
        """Whether the journal's bytes are in ``path`` where the write put them."""
        try:
            data = self.journal_file(pair).read_bytes()
        except FileNotFoundError:
            return False
        if hashlib.sha256(data).hexdigest() != p.sha256:
            return False
        intent = Intent(p.side, path, p.base, data, tuple(p.ids), p.before)
        return tail_state(path, intent) in ("complete", "followed")

    def _ids_present(self, pair: Pair, p: Pending) -> bool:
        if p.side == "codex":
            index = self.codex.index(pair.codex)
            return index.holds(tuple(p.ids), p.base + p.length)
        cpath = find_transcript(self.config.claude_home, pair.claude)
        if cpath is None:
            return False
        have = {ln.record.get("uuid") for ln in iter_lines(cpath)}
        return all(u in have for u in p.ids)

    # ------------------------------------------------------------------------------- helpers
    def paths(self, pair: Pair) -> tuple[Path, Path]:
        cpath = find_transcript(self.config.claude_home, pair.claude)
        if cpath is None:
            raise Blocked(f"the Claude session {pair.claude} is not on this machine any more")
        return cpath, self.codex_path(pair)

    def codex_path(self, pair: Pair) -> Path:
        xpath = self.codex.rollout(pair.codex)
        if xpath is None:
            raise Blocked(f"the Codex thread {pair.codex} is not on this machine any more")
        if is_compressed(xpath):
            raise Wait(
                "Codex compressed the idle thread; it syncs again once it is opened in Codex"
            )
        return xpath

    def codex_steps(self, pair: Pair, xlines: list[Line]) -> list[ClaudeStep]:
        if not xlines:
            return []
        return codex_to_claude_steps(
            [ln.record for ln in xlines], key=pair.key, outputs=self.outputs
        )

    def claude_live(self, pair: Pair):
        """The check that no Claude Code has the session, for the append to run at write time."""
        return lambda: live_pid(self.config.claude_home, pair.claude)

    def claude_stopped(self, pair: Pair, cpath: Path) -> bool:
        """No Claude Code has the session, and it has been quiet: a turn it left unfinished will
        not be finished."""
        quiet = time.time() - cpath.stat().st_mtime >= self.config.quiet_seconds
        return quiet and live_pid(self.config.claude_home, pair.claude) is None

    def codex_gate(self) -> None:
        """Refuse to write to a Codex whose item format is untested. The result is kept per
        Codex version across runs, and its alert is sent once."""
        if self._format is None:
            self._format, first_time = cached_format(
                self.codex, self.config.allow_untested_codex, self.config.home / "gate.json"
            )
            if self._format.problem and first_time:
                self.notify(f"duologue: {self._format.problem}")
        if self._format.problem:
            raise Wait(self._format.problem)

    def journal_file(self, pair: Pair) -> Path:
        name = hashlib.sha256(pair.key.encode()).hexdigest()[:16]
        return self.config.journal_dir / f"{name}.bin"

    def block(self, pair: Pair, reason: str) -> None:
        pair.blocked = reason
        self.log.write(pair.name, "blocked", reason)
        self.notify(f'duologue: "{_printable(pair.name)}" stopped syncing: {reason}')

    def notify(self, text: str) -> None:
        if not self.config.notify:
            return
        try:
            subprocess.run([*self.config.notify, text], timeout=30, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            self.log.write(None, "notify-failed", f"{type(e).__name__}: {e}")

    def save(self) -> None:
        self.store.save(self.pairs)


class PairJournal:
    """Records an append's intent in the pair (and its bytes in a journal file) before it
    happens, with the cursors that become true once it is confirmed."""

    def __init__(self, engine: Engine, pair: Pair, *, claude: dict | None, codex: dict | None):
        self.engine, self.pair = engine, pair
        self.claude, self.codex = claude, codex

    def begin(self, intent: Intent) -> None:
        written = {"end": intent.end, "anchor": intent.ids[-1] if intent.ids else None}
        write_atomic(self.engine.journal_file(self.pair), intent.data)
        self.pair.pending = Pending(
            side=intent.side,
            path=str(intent.path),
            base=intent.base,
            length=len(intent.data),
            sha256=intent.digest,
            ids=list(intent.ids),
            claude=written if intent.side == "claude" else (self.claude or {}),
            codex=written if intent.side == "codex" else (self.codex or {}),
            before=intent.before,
        )
        self.engine.save()


# ------------------------------------------------------------------------- starting points
def claude_start(cpath: Path) -> Cursor:
    """Where syncing a Claude session starts: its end, or the start of a turn Claude is still
    working on (that turn is copied once it ends)."""
    end = anchor = None
    last_prompt_start = None
    previous = 0
    lines = 0
    for ln in iter_lines(cpath):
        lines += 1
        if is_typed(ln.record):
            last_prompt_start = (previous, anchor)
        if ln.record.get("uuid"):
            anchor = ln.record["uuid"]
        previous = ln.end
    end = previous
    if lines and not settled(iter_lines(cpath)) and last_prompt_start is not None:
        end, anchor = last_prompt_start
    return Cursor(end, prefix_hash(cpath, end), anchor)


def codex_start(xpath: Path) -> Cursor:
    """Where syncing a Codex thread starts: after its last finished turn."""
    ends = turn_ends(iter_lines(xpath))
    if not ends:
        return Cursor(0, hashlib.sha256(b"").hexdigest(), None)
    turn, end = ends[-1]
    return Cursor(end, prefix_hash(xpath, end), turn)


def end_cursor(path: Path) -> Cursor:
    """A cursor at the end of the whole lines of ``path``."""
    end, anchor = 0, None
    for ln in iter_lines(path):
        end = ln.end
        anchor = ln.record.get("uuid") or anchor
    return Cursor(end, prefix_hash(path, end), anchor)


# ---------------------------------------------------------------------------------- helpers
def _stamp(path: Path) -> list:
    st = path.stat()
    return [str(path), st.st_size, st.st_mtime_ns]


def _matches(path: Path, cur: Cursor) -> bool:
    return path.stat().st_size >= cur.end and prefix_hash(path, cur.end) == cur.digest


def _check_record_types(lines: Sequence[Line]) -> None:
    """Fail closed on a chain record of a type duologue does not know that carries a message:
    Claude Code changed its format, and copying would silently drop what it says. Records
    without a uuid (``api-request-blob`` and other bookkeeping) are never conversation."""
    for ln in lines:
        kind = ln.record.get("type")
        if ln.record.get("uuid") and "message" in ln.record and kind not in KNOWN_RECORD_TYPES:
            raise Blocked(f"the Claude transcript has a {kind!r} record duologue does not know")


def _last_uuid(lines: Sequence[Line]) -> str | None:
    for ln in reversed(lines):
        if ln.record.get("uuid"):
            return ln.record["uuid"]
    return None


def _printable(text: str) -> str:
    """``text`` without control characters, safe to print or hand to a notifier."""
    return "".join(ch if ch.isprintable() else " " for ch in text)
