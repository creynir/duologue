"""Codex's per-thread writer lock, taken exactly the way Codex takes it.

Codex (codex-rs ``rollout/src/writer_lock.rs``) keeps ``<CODEX_HOME>/thread-writer-locks/<id>.lock``
flock-ed for as long as a process has the thread loaded. Creating, probing and removing a thread's
lock file all happen under ``.coordination.lock`` in the same folder, which every writer waits for
up to 30 seconds. While duologue holds a thread's lock, Codex refuses to load the thread ("already
has an active writer"), so nothing else writes to the rollout during an append.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

from duologue.ids import require_id

LOCK_DIR = "thread-writer-locks"
COORDINATION = ".coordination.lock"
COORDINATION_WAIT = 30.0  # seconds; what Codex itself waits


@contextlib.contextmanager
def _coordination(directory: Path, wait: float = COORDINATION_WAIT) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / COORDINATION, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex's writer coordination lock stayed taken") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


class WriterLock:
    """Exclusive ownership of one thread's rollout."""

    def __init__(self, codex_home: Path, thread_id: str):
        self.directory = Path(codex_home) / LOCK_DIR
        self.path = self.directory / f"{require_id(thread_id, 'thread id')}.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock; False when another process holds it."""
        if self._fd is not None:
            return True
        with _coordination(self.directory):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            self._fd = fd
        return True

    def release(self) -> None:
        """Give the lock up and remove its file, as Codex does when a writer finishes. The lock
        is given up even when the coordination lock cannot be had; its file then stays, and
        Codex removes such stale files itself."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            with _coordination(self.directory):
                os.close(fd)
                fd = None
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
        finally:
            if fd is not None:
                os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> WriterLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def is_held(codex_home: Path, thread_id: str) -> bool:
    """Whether some process holds the thread's writer lock now (Codex: the thread is loaded).

    The probe runs under the coordination lock, as Codex's own probe does, so a Codex process
    that wants the thread at that moment waits for it instead of failing.
    """
    directory = Path(codex_home) / LOCK_DIR
    path = directory / f"{require_id(thread_id, 'thread id')}.lock"
    if not path.exists():
        return False
    with _coordination(directory):
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
    return False
