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
from duologue.claude.transcript import find_transcript, settled, tail
from duologue.codex import threads as codex_threads
from duologue.codex.rollout import is_compressed, session_meta, turn_ends
from duologue.codex.rpc import AppServerError
from duologue.codex.threads import Codex
from duologue.config import Config
from duologue.gate import CodexFormat, codex_format
from duologue.journal import Blocked, Intent, Wait, prefix_hash
from duologue.jsonl import Line, iter_lines, read_lines
from duologue.pairs import ActivityLog, Cursor, Pair, Pending, Store, now_iso, write_atomic
from duologue.translate.claude_to_codex import claude_to_codex, has_content
from duologue.translate.codex_to_claude import ClaudeStep, claude_records, codex_to_claude_steps
from duologue.translate.outputs import OutputStore

CONFLICT = (
    "you wrote in both Claude and Codex since the last sync, so nothing was copied; keep one "
    'side\'s new turns with: duologue resolve "{name}" --keep claude (or --keep codex)'
)
KNOWN_RECORD_TYPES = ("user", "assistant")  # the only record types that carry a message
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
        """One pass over ``pair``; saves the state and reports what happened."""
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
        except TRANSIENT as e:
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
        ccur = self.claude_cursor(pair, cpath)
        xcur = self.codex_cursor(pair, xpath)
        clines = read_lines(cpath, ccur.end)
        xall = read_lines(xpath, xcur.end)
        ends = turn_ends(xall)
        xlines = [ln for ln in xall if ln.end <= ends[-1][1]] if ends else []
        _check_record_types(clines)
        claude_new = has_content(ln.record for ln in clines)
        codex_steps = self.codex_steps(pair, xlines)
        if claude_new and codex_steps:
            pair.conflict = True
            text = CONFLICT.format(name=pair.name)
            self.log.write(pair.name, "conflict", text)
            self.notify(f'duologue: "{pair.name}": {text}')
            return Outcome("conflict", text)
        if claude_new:
            return self.to_codex(pair, cpath, clines)
        if codex_steps:
            return self.to_claude(pair, cpath, xlines, codex_steps)
        pair.seen = seen
        return Outcome("idle", "up to date")

    # ---------------------------------------------------------------------------- directions
    def to_codex(self, pair: Pair, cpath: Path, clines: list[Line]) -> Outcome:
        if not settled(clines) and not self.claude_stopped(pair, cpath):
            raise Wait("Claude is still working on its turn")
        if time.time() - cpath.stat().st_mtime < self.config.quiet_seconds:
            raise Wait("Claude's turn just ended; it is copied once the session is quiet")
        self.codex_gate()
        cwd = tail(iter_lines(cpath)).cwd or session_meta(self.codex_path(pair)).get("cwd", "")
        lines = claude_to_codex(
            [ln.record for ln in clines],
            thread_id=pair.codex,
            key=pair.key,
            outputs=self.outputs,
            default_cwd=cwd,
        )
        after_claude = {"end": clines[-1].end, "anchor": _last_uuid(clines)}
        journal = PairJournal(self, pair, claude=after_claude, codex=None)
        done = codex_threads.append_turns(self.codex, pair.codex, lines, journal=journal)
        self.confirm(pair, after_claude, {"end": done.end, "anchor": done.turns[-1]})
        pair.copied["to_codex"] += len(done.turns)
        text = f"copied {len(done.turns)} Claude turn(s) to Codex"
        self.log.write(pair.name, "to_codex", text if done.written else text + " (already there)")
        return Outcome("synced", text)

    def to_claude(
        self, pair: Pair, cpath: Path, xlines: list[Line], steps: list[ClaudeStep]
    ) -> Outcome:
        live = self.claude_live(pair)
        if live() is not None:
            raise Wait("the Claude session is open; Codex's turns are copied to it once it closes")
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
        done = claude_sessions.append_records(cpath, records, journal=journal, live=live)
        self.confirm(pair, {"end": done.end, "anchor": done.uuids[-1]}, after_codex)
        pair.copied["to_claude"] += len(turns)
        text = f"copied {len(turns)} Codex turn(s) to Claude"
        self.log.write(pair.name, "to_claude", text if done.written else text + " (already there)")
        return Outcome("synced", text)

    # ------------------------------------------------------------------------------- cursors
    def claude_cursor(self, pair: Pair, cpath: Path) -> Cursor:
        cur = pair.claude_cursor
        if _matches(cpath, cur):
            return cur
        raise Blocked(
            "the Claude transcript changed before the point already synced; check it, then "
            "run duologue release to sync on from where it stands"
        )

    def codex_cursor(self, pair: Pair, xpath: Path) -> Cursor:
        cur = pair.codex_cursor
        if _matches(xpath, cur):
            return cur
        if cur.anchor:  # Codex rewrites a whole thread when it converts it: find the turn again
            hits = [end for turn, end in turn_ends(read_lines(xpath)) if turn == cur.anchor]
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

    def confirm(self, pair: Pair, claude: dict | None, codex: dict | None) -> None:
        """Move the cursors past a confirmed write, and forget its intent."""
        cpath, xpath = self.paths(pair)
        if claude is not None:
            pair.claude_cursor = Cursor(
                claude["end"], prefix_hash(cpath, claude["end"]), claude["anchor"]
            )
        if codex is not None:
            pair.codex_cursor = Cursor(
                codex["end"], prefix_hash(xpath, codex["end"]), codex["anchor"]
            )
        self.forget_pending(pair)
        pair.last_sync = now_iso()

    def settle(self, pair: Pair) -> None:
        """Settle an append an earlier pass did not confirm."""
        p = pair.pending
        assert p is not None
        try:
            data = self.journal_file(pair).read_bytes()
        except FileNotFoundError:
            raise Blocked("the journal of an interrupted write is missing") from None
        if hashlib.sha256(data).hexdigest() != p.sha256:
            raise Blocked("the journal of an interrupted write does not match its record")
        intent = Intent(p.side, Path(p.path), p.base, data, tuple(p.ids))
        if p.side == "codex":
            done = codex_threads.recover(self.codex, pair.codex, intent)
        else:
            done = claude_sessions.recover(Path(p.path), intent)
        if done:
            self.confirm(pair, p.claude or None, p.codex or None)
            self.log.write(pair.name, "recovered", f"an interrupted write to {p.side} is complete")
        else:
            self.forget_pending(pair)
            self.log.write(pair.name, "recovered", f"an interrupted write to {p.side} was undone")
        self.save()

    def forget_pending(self, pair: Pair) -> None:
        pair.pending = None
        self.journal_file(pair).unlink(missing_ok=True)

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
        if self._format is None:
            self._format = codex_format(self.codex, self.config.allow_untested_codex)
            if self._format.problem:
                self.notify(f"duologue: {self._format.problem}")
        if self._format.problem:
            raise Wait(self._format.problem)

    def journal_file(self, pair: Pair) -> Path:
        name = hashlib.sha256(pair.key.encode()).hexdigest()[:16]
        return self.config.journal_dir / f"{name}.bin"

    def block(self, pair: Pair, reason: str) -> None:
        pair.blocked = reason
        self.log.write(pair.name, "blocked", reason)
        self.notify(f'duologue: "{pair.name}" stopped syncing: {reason}')

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
        )
        self.engine.save()


# ---------------------------------------------------------------------------------- helpers
def _stamp(path: Path) -> list:
    st = path.stat()
    return [str(path), st.st_size, st.st_mtime_ns]


def _matches(path: Path, cur: Cursor) -> bool:
    return path.stat().st_size >= cur.end and prefix_hash(path, cur.end) == cur.digest


def _check_record_types(lines: Sequence[Line]) -> None:
    """Fail closed on a record type duologue does not know that carries a message: Claude Code
    changed its format, and copying would silently drop what it says."""
    for ln in lines:
        kind = ln.record.get("type")
        if "message" in ln.record and kind not in KNOWN_RECORD_TYPES:
            raise Blocked(f"the Claude transcript has a {kind!r} record duologue does not know")


def _last_uuid(lines: Sequence[Line]) -> str | None:
    for ln in reversed(lines):
        if ln.record.get("uuid"):
            return ln.record["uuid"]
    return None
