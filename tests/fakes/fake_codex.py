"""A stand-in for the ``codex`` binary in integration tests.

It does what duologue relies on Codex for, the way Codex 0.166 does it, and no more:

- ``app-server``: JSON-RPC over stdio. ``thread/resume`` takes the thread's writer lock (held
  until the process exits or the thread is archived) and indexes the rollout's lines past the
  checkpoint into ``thread_history_1.sqlite``; ``thread/loaded/list``, ``thread/archive``,
  ``thread/unarchive`` and ``thread/unsubscribe`` behave like Codex's.
- ``migrate-rollouts --thread <id> --apply --json``: converts a legacy rollout to paginated.
- ``app-server daemon-serve`` (fake only) runs a shared app-server on the home's control socket,
  and ``app-server proxy`` connects stdio to it, as ``codex app-server proxy`` does.
- ``--version``.

Like Codex, it creates its history index when it starts, and a thread's status in
``thread/read`` is ``notLoaded``, ``idle`` (loaded here) or ``active`` (loaded with a turn open).
Lock files are created and removed under ``.coordination.lock``, as codex-rs does.

Knobs, through the environment: ``FAKE_CODEX_NO_INDEX=1`` makes resume index nothing,
``FAKE_CODEX_INDEX_TURNS=<n>`` makes it index only the first n turns it finds,
``FAKE_CODEX_LEAVE_LINES=<n>`` leaves the last n whole lines unindexed, and
``FAKE_CODEX_LOG`` names a file that gets one line per request.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sqlite3
import sys
import threading
from pathlib import Path

HOME = Path(os.environ["CODEX_HOME"])
SQLITE = Path(os.environ.get("CODEX_SQLITE_HOME") or HOME)
LOCKS = HOME / "thread-writer-locks"
SCHEMA = """
create table if not exists thread_turns (
    thread_id text not null, turn_id text not null, rollout_ordinal integer not null,
    status text not null, rollout_byte_offset integer, rollout_end_byte_offset integer,
    primary key (thread_id, turn_id));
create table if not exists thread_history_projection_state (
    thread_id text primary key, next_rollout_byte_offset integer not null,
    next_rollout_ordinal integer not null);
"""


def rollout(thread_id: str) -> Path | None:
    found = list(HOME.glob(f"*sessions/**/rollout-*-{thread_id}.jsonl"))
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


class Server:
    def __init__(self) -> None:
        self.locks: dict[str, int] = {}
        self.moved: dict[str, Path] = {}  # archived rollouts and where they came from

    def lock(self, thread_id: str) -> None:
        if thread_id in self.locks:
            return
        LOCKS.mkdir(parents=True, exist_ok=True)
        coord = os.open(LOCKS / ".coordination.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(coord, fcntl.LOCK_EX)
        try:
            fd = os.open(LOCKS / f"{thread_id}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RpcError(-32600, f"thread {thread_id} already has an active writer") from None
            self.locks[thread_id] = fd
        finally:
            os.close(coord)

    def unload(self, thread_id: str) -> None:
        fd = self.locks.pop(thread_id, None)
        if fd is None:
            return
        coord = os.open(LOCKS / ".coordination.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(coord, fcntl.LOCK_EX)
        try:
            os.close(fd)
            (LOCKS / f"{thread_id}.lock").unlink(missing_ok=True)
        finally:
            os.close(coord)

    def handle(self, method: str, params: dict):
        tid = params.get("threadId")
        if method == "initialize":
            return {"userAgent": "fake-codex"}
        if method == "thread/resume":
            path = rollout(tid)
            if path is None:
                raise RpcError(-32600, f"no rollout found for thread id {tid}")
            self.lock(tid)
            if not os.environ.get("FAKE_CODEX_NO_INDEX"):
                index(tid, path)
            return {"thread": {"id": tid, "path": str(path)}}
        if method == "thread/read":
            path = rollout(tid)
            if path is None:
                raise RpcError(-32600, f"no rollout found for thread id {tid}")
            status = "notLoaded"
            if tid in self.locks:
                status = "active" if turn_open(path) else "idle"
            return {"thread": {"id": tid, "status": {"type": status}}}
        if method == "thread/loaded/list":
            return {"data": sorted(self.locks), "nextCursor": None}
        if method == "thread/archive":
            self.unload(tid)
            path = rollout(tid)  # Codex moves an archived rollout to archived_sessions/
            if path is not None and "archived_sessions" not in path.parts:
                moved = HOME / "archived_sessions" / path.name
                moved.parent.mkdir(exist_ok=True)
                self.moved[tid] = path
                path.rename(moved)
            return {}
        if method == "thread/unarchive":
            path, back = rollout(tid), self.moved.pop(tid, None)
            if path is not None and back is not None:
                path.rename(back)
            return {"thread": {"id": tid}}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        raise RpcError(-32601, f"method not found: {method}")


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def turn_open(path: Path) -> bool:
    state = False
    for raw in path.read_bytes().split(b"\n")[:-1]:
        try:
            p = json.loads(raw).get("payload") or {}
        except ValueError:
            continue
        if p.get("type") == "task_started":
            state = True
        elif p.get("type") in ("task_complete", "turn_aborted"):
            state = False
    return state


def create_index() -> None:
    con = sqlite3.connect(SQLITE / "thread_history_1.sqlite")
    con.executescript(SCHEMA)
    con.close()


def index(thread_id: str, path: Path) -> None:
    """Codex's projector, reduced: index whole lines past the checkpoint; ordinals that go
    backwards are skipped. With FAKE_CODEX_INDEX_TURNS, it stops before the turn after the
    first n it finds."""
    limit = os.environ.get("FAKE_CODEX_INDEX_TURNS")
    seen = 0
    con = sqlite3.connect(SQLITE / "thread_history_1.sqlite")
    con.executescript(SCHEMA)
    row = con.execute(
        "select next_rollout_byte_offset, next_rollout_ordinal "
        "from thread_history_projection_state where thread_id = ?",
        (thread_id,),
    ).fetchone()
    offset, ordinal = row if row else (0, 0)
    data = path.read_bytes()
    open_turn = None
    raws = data[offset:].split(b"\n")[:-1]
    leave = int(os.environ.get("FAKE_CODEX_LEAVE_LINES") or 0)
    for raw in raws[: len(raws) - leave] if leave else raws:
        start = offset
        try:
            line = json.loads(raw)
        except ValueError:
            offset += len(raw) + 1
            continue
        p = line.get("payload") or {}
        if p.get("type") == "task_started" and limit is not None and seen >= int(limit):
            break
        seen += p.get("type") == "task_started"
        offset += len(raw) + 1
        n = line.get("ordinal", ordinal)
        if n < ordinal:
            continue
        ordinal = n + 1
        if line.get("type") != "event_msg":
            continue
        if p.get("type") == "task_started":
            open_turn = p["turn_id"]
            con.execute(
                "insert into thread_turns values (?, ?, ?, 'inProgress', ?, null)",
                (thread_id, open_turn, n, start),
            )
        elif p.get("type") in ("task_complete", "turn_aborted") and p.get("turn_id"):
            status = "completed" if p["type"] == "task_complete" else "interrupted"
            con.execute(
                "update thread_turns set status = ?, rollout_end_byte_offset = ? "
                "where thread_id = ? and turn_id = ?",
                (status, offset, thread_id, p["turn_id"]),
            )
    con.execute(
        "insert or replace into thread_history_projection_state values (?, ?, ?)",
        (thread_id, offset, ordinal),
    )
    con.commit()
    con.close()


def migrate(thread_id: str) -> str:
    path = rollout(thread_id)
    if path is None:
        return "not_found"
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    meta = lines[0].get("payload") or {}
    if meta.get("history_mode") == "paginated":
        return "already_paginated"
    meta["history_mode"] = "paginated"
    out = [dict(x, ordinal=k) for k, x in enumerate(lines)]
    path.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in out))
    return "migrated"


def serve(server: Server, lines, write, guard: threading.Lock | None = None) -> None:
    log = os.environ.get("FAKE_CODEX_LOG")
    for raw in lines:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        if log:
            with open(log, "a") as fh:
                fh.write(
                    json.dumps({"method": msg.get("method"), "params": msg.get("params")}) + "\n"
                )
        try:
            with guard or threading.Lock():
                result = server.handle(msg["method"], msg.get("params") or {})
            reply = {"id": msg["id"], "result": result}
        except RpcError as e:
            reply = {"id": msg["id"], "error": {"code": e.code, "message": e.message}}
        write(json.dumps(reply) + "\n")


def control_socket() -> Path:
    return HOME / "app-server-control" / "app-server-control.sock"


def daemon() -> None:
    """A shared app-server: every connection to the control socket talks to the same server."""
    create_index()
    server, guard = Server(), threading.Lock()
    path = control_socket()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # a socket file a stopped daemon left behind
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    print("listening", flush=True)

    def client(conn: socket.socket) -> None:
        with conn, conn.makefile("r") as reader, conn.makefile("w") as writer:

            def write(text: str) -> None:
                writer.write(text)
                writer.flush()

            serve(server, reader, write, guard)

    while True:
        conn, _ = listener.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def proxy() -> int:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(str(control_socket()))
    except OSError as e:
        print(f"Error: failed to connect to socket: {e}", file=sys.stderr)
        return 1

    def upstream() -> None:
        for raw in sys.stdin:
            conn.sendall(raw.encode())
        conn.shutdown(socket.SHUT_WR)

    threading.Thread(target=upstream, daemon=True).start()
    with conn.makefile("r") as reader:
        for line in reader:
            sys.stdout.write(line)
            sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print("codex-cli 0.166.0")
        return 0
    if argv == ["app-server"]:
        create_index()
        server = Server()

        def write(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        serve(server, sys.stdin, write)
        for thread_id in list(server.locks):  # a Codex that exits gives its threads up
            server.unload(thread_id)
        return 0
    if argv == ["app-server", "proxy"]:
        return proxy()
    if argv == ["app-server", "daemon-serve"]:
        daemon()
        return 0
    if argv[:1] == ["migrate-rollouts"]:
        tid = argv[argv.index("--thread") + 1]
        print(json.dumps({"outcomes": [{"thread_id": tid, "status": migrate(tid)}]}))
        return 0
    print(f"fake codex: unsupported arguments {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
