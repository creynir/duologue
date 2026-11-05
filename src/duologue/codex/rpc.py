"""JSON-RPC to a Codex app-server over stdio, one JSON message per line.

When an app-server daemon runs (``codex remote-control start`` or ``codex app-server daemon``),
duologue talks to it through ``codex app-server proxy``, so it acts on the same loaded threads the
apps see. Otherwise it starts its own ``codex app-server`` for the call and stops it afterwards,
which also gives up every thread lock that server took.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import queue
import socket
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from duologue import __version__

CONTROL_SOCKET = ("app-server-control", "app-server-control.sock")
ACTIVE_WRITER = "already has an active writer"


class AppServerError(RuntimeError):
    """The app-server answered a request with an error, or stopped answering."""

    def __init__(self, method: str, error: Mapping):
        self.method = method
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(f"{method}: {error.get('message') or error}")

    @property
    def active_writer(self) -> bool:
        """Another process has the thread loaded."""
        return ACTIVE_WRITER in str(self)


class AppServer:
    """A running app-server connection. Use it as a context manager, or call :meth:`close`."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike | None = None,
        timeout: float = 120.0,
        shared: bool = False,
    ):
        self.timeout = timeout
        self.shared = shared  # a daemon other clients use too, not a private server
        self._proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._readers = [
            threading.Thread(target=self._pump, daemon=True),
            threading.Thread(target=self._drain, daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        self._next_id = 0
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "duologue", "title": "duologue", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------------------- requests
    def request(self, method: str, params: Mapping | None = None, timeout: float | None = None):
        """Send a request and return its result; raise :class:`AppServerError` on an error."""
        self._next_id += 1
        rid = self._next_id
        self._send({"id": rid, "method": method, "params": dict(params or {})})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: the Codex app-server did not answer in time")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                tail = " | ".join(self._stderr)
                raise AppServerError(
                    method, {"message": f"the Codex app-server exited ({tail or 'no output'})"}
                )
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise AppServerError(method, msg.get("error") or {})
                return msg.get("result")
            if "method" in msg and "id" in msg:
                # The server asks something of the client (an approval): duologue grants nothing.
                self._send(
                    {
                        "id": msg["id"],
                        "error": {"code": -32601, "message": "duologue does not handle this"},
                    }
                )

    def _send(self, msg: Mapping) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.closed:
            raise AppServerError(str(msg.get("method")), {"message": "the connection is closed"})
        try:
            stdin.write(json.dumps(msg) + "\n")
            stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise AppServerError(str(msg.get("method")), {"message": str(e)}) from e

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _drain(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            if line.strip():
                self._stderr.append(line.strip()[:300])

    # ---------------------------------------------------------------------------------- closing
    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        try:
            self._proc.wait(10)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        for reader in self._readers:  # they end with the process's output
            reader.join(5)
        for stream in (self._proc.stdout, self._proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                if stream is not None:
                    stream.close()

    def __enter__(self) -> AppServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def control_socket(codex_home: Path) -> Path:
    return Path(codex_home).joinpath(*CONTROL_SOCKET)


def daemon_running(codex_home: Path) -> bool:
    """Whether an app-server daemon listens on the home's control socket."""
    path = control_socket(codex_home)
    if not path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(str(path))
    except OSError:
        return False
    finally:
        s.close()
    return True
