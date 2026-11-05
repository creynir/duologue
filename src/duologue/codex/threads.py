"""Appending turns to a Codex thread, and creating one.

Codex shows a paginated thread only from its history index, and indexes a rollout's new lines
itself when the thread is resumed ("Paginated JSONL is canonical, but its SQLite projection can
lag", codex-rs ``thread_processor``). So an append never touches Codex's databases:

1. A thread that is loaded somewhere is not written to. In a shared app-server daemon, duologue
   closes it first (``thread/archive`` + ``thread/unarchive``) when it is idle there; otherwise
   it waits. An archived thread is left alone until it is unarchived.
2. A legacy thread is converted with ``codex migrate-rollouts`` first.
3. Codex's index must end where the file ends. If it lags, a resume lets Codex catch up first.
4. Under the thread's writer lock, the journal records the intent, then the lines go on with
   ordinals that continue the index.
5. ``thread/resume`` (no overrides: they would persist on the thread) makes Codex index them.
6. The index must then hold every new turn. When Codex indexed none of them and nothing was
   written after them, the lines are taken out again; either way the pair is blocked.

Only duologue's own bytes are ever taken out of a rollout: never bytes Codex indexed or wrote
after them. Any exception after the intent was recorded leaves it for :func:`recover`; a caller
settles a recorded intent before it appends again.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from duologue.codex import lock
from duologue.codex.history import HistoryIndex, read_index
from duologue.codex.rollout import (
    TURN_ENDS,
    find_rollout,
    history_mode,
    is_archived,
    is_compressed,
    model_provider,
    new_rollout_path,
    sqlite_home,
    uuid7,
)
from duologue.codex.rpc import AppServer, AppServerError, daemon_running
from duologue.ids import require_id
from duologue.journal import Blocked, Intent, Journal, Wait, prefix_hash, tail_state
from duologue.jsonl import dumps, ends_with_newline, iter_lines
from duologue.translate.claude_to_codex import RolloutLine
from duologue.translate.common import iso_ms

MIGRATED = ("migrated", "already_paginated")


@dataclass(frozen=True)
class Codex:
    """Where Codex keeps its threads, and how to run it."""

    home: Path
    bin: str = "codex"
    env: Mapping[str, str] | None = None  # added to the environment of every codex process
    timeout: float = 120.0

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.env or {})
        env["CODEX_HOME"] = str(self.home)
        return env

    def app_server(self) -> AppServer:
        """The daemon's app-server through ``codex app-server proxy`` when one runs, else a
        private one that stops (and gives up its thread locks) when closed."""
        shared = daemon_running(self.home)
        argv = [self.bin, "app-server", "proxy"] if shared else [self.bin, "app-server"]
        return AppServer(
            argv, env=self.environment(), cwd=self.home, timeout=self.timeout, shared=shared
        )

    @property
    def sqlite(self) -> Path:
        """Where this Codex keeps its databases."""
        return sqlite_home(self.home, self.environment())

    def rollout(self, thread_id: str) -> Path | None:
        return find_rollout(self.home, thread_id, self.sqlite)

    def index(self, thread_id: str) -> HistoryIndex:
        return read_index(self.sqlite, thread_id)

    def run(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.bin, *args],
            capture_output=True,
            text=True,
            env=self.environment(),
            cwd=self.home,
            timeout=timeout or self.timeout,
        )

    def version(self) -> str | None:
        """Codex's version, such as ``0.166.0``; None when it does not run."""
        try:
            out = self.run("--version", timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"(\d+\.\d+\.\d+\S*)", out)
        return match.group(1) if match else None

    def migrate(self, thread_id: str) -> str:
        """Convert a legacy thread to paginated history with Codex's own migration; its outcome,
        such as ``migrated``, ``already_paginated`` or ``skipped_busy``."""
        try:
            r = self.run(
                "migrate-rollouts", "--thread", thread_id, "--apply", "--json", timeout=300
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"failed: {e}"
        try:
            return str(json.loads(r.stdout)["outcomes"][0]["status"])
        except (ValueError, KeyError, IndexError, TypeError):
            tail = (r.stderr or r.stdout).strip()[-200:]
            return f"failed: {tail or 'no output'}"


@dataclass(frozen=True)
class Appended:
    """The outcome of an append: ``[start, end)`` of ``path`` holds ``turns``."""

    path: Path
    start: int
    end: int
    turns: tuple[str, ...]
    written: bool  # False: the turns were already there (a retry), nothing was written


def turn_ids(lines: Sequence[RolloutLine]) -> tuple[str, ...]:
    return tuple(
        str(ln.payload.get("turn_id"))
        for ln in lines
        if ln.type == "event_msg" and ln.payload.get("type") == "task_started"
    )


def encode(lines: Sequence[RolloutLine], first_ordinal: int) -> bytes:
    """Rollout lines as Codex writes them: compact JSON, one per line, ordinals in sequence."""
    out = []
    for k, ln in enumerate(lines):
        record = {
            "timestamp": iso_ms(ln.ms),
            "ordinal": first_ordinal + k,
            "type": ln.type,
            "payload": ln.payload,
        }
        out.append(dumps(record, compact=True) + "\n")
    return "".join(out).encode()


# ------------------------------------------------------------------------------------- append
def append_turns(
    codex: Codex,
    thread_id: str,
    lines: Sequence[RolloutLine],
    *,
    journal: Journal,
    expect_size: int | None = None,
) -> Appended:
    """Append ``lines`` (whole turns from the translator) to the thread and have Codex index
    them. Raises :class:`Wait` when the thread is busy and :class:`Blocked` when it needs a
    person; see the module docstring for what an exception after the write means.

    ``expect_size`` is the size the caller read the rollout at: when the file has grown since
    (a turn finished meanwhile), nothing is written, so the caller can look at it first."""
    require_id(thread_id, "thread id")
    turns = turn_ids(lines)
    if len(set(turns)) != len(turns):
        raise Blocked("the turns to add repeat a turn id; Codex would keep only one of them")
    path = _rollout(codex, thread_id)
    if not lines:
        size = path.stat().st_size
        return Appended(path, size, size, (), False)
    if expect_size is not None and path.stat().st_size != expect_size:
        # Checked before converting a legacy thread: the conversion rewrites the file itself.
        raise Wait("the Codex thread changed while duologue prepared its turns")
    if not ends_with_newline(path):
        raise Wait("Codex is writing to the thread")
    if _is_held(codex, thread_id):
        if last_turn_open(path):
            raise Wait("a Codex turn is running in the thread")
        if not _close_in_daemon(codex, thread_id):
            raise Wait("the thread is open in Codex")
        path = _rollout(codex, thread_id)
    if last_turn_open(path):
        # Nobody has the thread loaded, so nobody works on that turn: a Codex that stopped left
        # it open (Codex 0.166 does not close it on resume). Its next turn closes it.
        raise Wait("the last Codex turn in the thread never finished; it syncs after the next one")
    path = _paginated(codex, thread_id, path)
    index, size = _caught_up(codex, thread_id, path)
    present = [t for t in turns if t in index.turns]
    if present:
        if len(present) == len(turns):
            start, end = _turn_range(path, turns)
            return Appended(path, start, end, turns, False)
        raise Blocked(
            f"{len(present)} of the {len(turns)} turns to add are already in the Codex thread"
        )
    data = encode(lines, index.next_ordinal or 0)
    with _writer(codex, thread_id):
        if path.stat().st_size != size:
            raise Wait("the Codex thread changed while duologue prepared its turns")
        intent = Intent("codex", path, size, data, turns, prefix_hash(path, size))
        journal.begin(intent)
        _write(path, intent)
    finish(codex, thread_id, intent)
    return Appended(path, size, intent.end, turns, True)


def finish(codex: Codex, thread_id: str, intent: Intent) -> None:
    """Have Codex index a write duologue made, and check that it did. A write Codex indexed none
    of is taken out again when nothing follows it; a failure raises :class:`Blocked`."""
    resume(codex, thread_id)
    path = codex.rollout(thread_id)
    if path is not None:
        intent = replace(intent, path=path)  # an archive round trip may have moved it
    index = codex.index(thread_id)
    if index.holds(intent.ids, intent.end):
        return
    if not index.found:
        raise Blocked(
            "Codex's history index was not found after the write; the thread was left as it is"
        )
    nothing = not any(t in index.turns for t in intent.ids)
    if nothing and index.next_offset in (None, intent.base):
        if _take_out(codex, thread_id, intent):
            raise Blocked(
                f"Codex did not index the {len(intent.ids)} turn(s) duologue added; "
                "they were taken out of the thread again"
            )
        raise Blocked(
            "Codex did not index the turns duologue added, and wrote after them; "
            "the thread was left as it is"
        )
    whole = sum(index.turns.get(t) not in (None, "inProgress") for t in intent.ids)
    raise Blocked(
        f"Codex indexed {whole} of the {len(intent.ids)} turns duologue added whole (index at "
        f"byte {index.next_offset}, the write ends at {intent.end}); the thread was left as it is"
    )


def recover(codex: Codex, thread_id: str, intent: Intent) -> bool:
    """Settle an append that was interrupted. True when it is complete and indexed now; False
    when nothing of it is in the thread (a cut-off part nobody indexed is taken out), so the
    caller can write it again."""
    require_id(thread_id, "thread id")
    path = codex.rollout(thread_id)
    if path is None:
        if intent.base == 0:
            return False  # a new thread whose file was never made
        raise Wait("the Codex thread's file is not there now")
    if is_compressed(path):
        raise Wait("the Codex thread's file is compressed now")
    intent = replace(intent, path=path)  # archiving moves the file
    state = tail_state(path, intent)
    if state in ("complete", "followed"):
        if codex.index(thread_id).holds(intent.ids, intent.end):
            return True
        finish(codex, thread_id, intent)
        return True
    if state == "absent":
        return False
    if state == "partial":
        index = codex.index(thread_id)
        if (index.next_offset or 0) > intent.base:
            raise Blocked(
                "Codex indexed part of an interrupted write by duologue; the thread was left "
                "as it is"
            )
        if _take_out(codex, thread_id, intent):
            return False
    raise Blocked("the Codex thread changed after an interrupted write by duologue")


def resume(codex: Codex, thread_id: str) -> None:
    """``thread/resume`` with no overrides (they would persist on the thread): Codex indexes
    whatever its index lacks. In a shared daemon, a thread duologue loaded is unloaded again."""
    with codex.app_server() as server:
        loaded_before = server.shared and _status(server, thread_id) != "notLoaded"
        try:
            server.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        except AppServerError as e:
            if e.active_writer:
                raise Wait("the thread is open in Codex") from e
            raise
    if server.shared and not loaded_before:
        _archive_round_trip(codex, thread_id)


# ------------------------------------------------------------------------------------- create
def create_thread(
    codex: Codex,
    *,
    cwd: str,
    build: Callable[[str], Sequence[RolloutLine]],
    journal: Journal,
    source: str = "vscode",
    created_ms: int | None = None,
    thread_id: str | None = None,
) -> tuple[str, Appended]:
    """Create a paginated thread holding the turns ``build(thread_id)`` returns, and have Codex
    index it. ``source`` is how the thread is listed; the Codex app lists ``vscode`` threads.
    ``thread_id`` (a UUIDv7, see :func:`~duologue.codex.rollout.uuid7`) lets the caller record
    the thread before it exists."""
    provider = model_provider(codex.home)
    if provider is None:
        raise Blocked("Codex's config.toml could not be read, so its model provider is unknown")
    created_ms = int(time.time() * 1000) if created_ms is None else created_ms
    thread_id = uuid7(created_ms) if thread_id is None else require_id(thread_id, "thread id")
    lines = list(build(thread_id))
    meta = {
        "session_id": thread_id,
        "id": thread_id,
        "timestamp": iso_ms(created_ms),
        "cwd": cwd,
        "runtime_workspace_roots": [cwd],
        "originator": "duologue",
        "cli_version": codex.version() or "",
        "source": source,
        "thread_source": "user",
        "model_provider": provider,
        "history_mode": "paginated",
    }
    first = {"timestamp": iso_ms(created_ms), "ordinal": 0, "type": "session_meta", "payload": meta}
    data = (dumps(first, compact=True) + "\n").encode() + encode(lines, 1)
    path = new_rollout_path(codex.home, thread_id, created_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    intent = Intent("codex", path, 0, data, turn_ids(lines))
    with _writer(codex, thread_id):
        journal.begin(intent)
        # Owner-only, like the Claude transcript it is made from.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    finish(codex, thread_id, intent)
    return thread_id, Appended(path, 0, len(data), intent.ids, True)


# ------------------------------------------------------------------------------------ helpers
def last_turn_open(path: Path) -> bool:
    """Whether the rollout's last turn started and has not finished. Only lines that start or
    end a turn are parsed."""
    state = False
    for ln in iter_lines(path):
        p = ln.record.get("payload")
        if ln.record.get("type") != "event_msg" or not isinstance(p, dict):
            continue
        if p.get("type") == "task_started":
            state = True
        elif p.get("type") in TURN_ENDS:
            state = False
    return state


def _rollout(codex: Codex, thread_id: str) -> Path:
    path = codex.rollout(thread_id)
    if path is None:
        raise Blocked(f"Codex thread {thread_id} has no rollout file")
    if is_compressed(path):
        raise Wait("Codex compressed the idle thread; it syncs again once the thread is opened")
    if is_archived(codex.home, path):
        raise Wait("the thread is archived in Codex; it syncs again once it is unarchived")
    return path


def _paginated(codex: Codex, thread_id: str, path: Path) -> Path:
    if history_mode(path) == "paginated":
        return path
    status = codex.migrate(thread_id)
    if status == "skipped_busy":
        raise Wait("Codex is busy with the thread; it converts it later")
    if status not in MIGRATED:
        raise Blocked(f"Codex could not convert the thread to paginated history ({status})")
    return _rollout(codex, thread_id)


def _caught_up(codex: Codex, thread_id: str, path: Path) -> tuple[HistoryIndex, int]:
    """The thread's index once it ends where the file ends; a resume lets Codex catch up."""
    index, size = codex.index(thread_id), path.stat().st_size
    if index.next_offset == size:
        return index, size
    resume(codex, thread_id)
    index, size = codex.index(thread_id), path.stat().st_size
    if index.next_offset != size:
        raise Blocked(
            f"Codex's index of the thread ends at byte {index.next_offset}, its file at {size}"
        )
    return index, size


def _turn_range(path: Path, turns: tuple[str, ...]) -> tuple[int, int]:
    """Where ``turns`` are in the rollout: from the start of the first to the end of the last."""
    start = end = None
    previous = 0
    for ln in iter_lines(path):
        p = ln.record.get("payload") if isinstance(ln.record.get("payload"), dict) else {}
        if ln.record.get("type") == "event_msg" and p.get("turn_id") in turns:
            if p.get("type") == "task_started" and start is None and p["turn_id"] == turns[0]:
                start = previous
            if p.get("type") in TURN_ENDS and p["turn_id"] == turns[-1]:
                end = ln.end
        previous = ln.end
    if start is None or end is None:
        raise Blocked("the turns Codex indexed are not whole in the thread's file")
    return start, end


def _is_held(codex: Codex, thread_id: str) -> bool:
    try:
        return lock.is_held(codex.home, thread_id)
    except TimeoutError as e:
        raise Wait("Codex's thread locks are busy") from e


@contextlib.contextmanager
def _writer(codex: Codex, thread_id: str) -> Iterator[None]:
    held = lock.WriterLock(codex.home, thread_id)
    try:
        if not held.acquire():
            raise Wait("the thread is open in Codex")
    except TimeoutError as e:
        raise Wait("Codex's thread locks are busy") from e
    try:
        yield
    finally:
        held.release()


def _write(path: Path, intent: Intent) -> None:
    """Append the intent's bytes; a write that fails partway is taken out again before the lock
    is released, so no torn line is left for Codex to write after."""
    with open(path, "ab") as fh:
        try:
            if fh.tell() != intent.base:
                raise Wait("the Codex thread changed while duologue wrote")
            fh.write(intent.data)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            if tail_state(path, intent) == "partial":
                os.truncate(path, intent.base)
            raise


def _take_out(codex: Codex, thread_id: str, intent: Intent) -> bool:
    """Remove duologue's bytes when they are all that follows ``base`` and Codex indexed none of
    them. False when that is no longer so (the file is left as it is)."""
    with _writer(codex, thread_id):
        if tail_state(intent.path, intent) not in ("complete", "partial"):
            return False
        if (codex.index(thread_id).next_offset or 0) > intent.base:
            return False
        if intent.base == 0:
            intent.path.unlink()
        else:
            os.truncate(intent.path, intent.base)
    return True


def _status(server: AppServer, thread_id: str) -> str | None:
    """The thread's status in an app-server: notLoaded, idle, active or systemError."""
    try:
        thread = server.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    except AppServerError:
        return None
    status = ((thread or {}).get("thread") or {}).get("status") or {}
    return status.get("type") if isinstance(status, dict) else None


def _close_in_daemon(codex: Codex, thread_id: str) -> bool:
    """Close the thread in the shared daemon when that is where it is loaded and it is idle.
    True when its writer lock is free afterwards."""
    if not daemon_running(codex.home):
        return False
    with codex.app_server() as server:
        if not server.shared or _status(server, thread_id) != "idle":
            return False  # not loaded there (another process holds it), or a turn is running
    _archive_round_trip(codex, thread_id)
    return not _is_held(codex, thread_id)


def _archive_round_trip(codex: Codex, thread_id: str) -> None:
    """Unload a thread from the shared daemon: archiving unloads it, unarchiving lists it again.
    Each unarchive attempt uses a new connection, so a dead one does not leave it archived."""
    with codex.app_server() as server:
        server.request("thread/archive", {"threadId": thread_id})
    for attempt in range(3):
        try:
            with codex.app_server() as server:
                server.request("thread/unarchive", {"threadId": thread_id})
            return
        except (AppServerError, TimeoutError, OSError):
            if attempt == 2:
                raise Blocked(
                    "duologue closed the Codex thread but could not unarchive it; "
                    "unarchive it in the Codex app"
                ) from None
            time.sleep(2)
