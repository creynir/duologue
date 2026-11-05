import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duologue.codex import lock
from duologue.codex.lock import WriterLock, is_held

THREAD = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f96ef"

# Holds a thread's lock the way Codex does until its stdin closes.
HOLDER = """
import fcntl, os, sys
d = sys.argv[1]
os.makedirs(d, exist_ok=True)
fd = os.open(os.path.join(d, sys.argv[2] + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def holder(tmp_path: Path):
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / lock.LOCK_DIR), THREAD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout.readline().strip() == "held"
    yield p
    p.stdin.close()
    p.wait(10)


def test_acquire_creates_the_lock_file_and_release_removes_it(tmp_path: Path):
    held = WriterLock(tmp_path, THREAD)
    assert held.acquire()
    assert held.held
    assert (tmp_path / lock.LOCK_DIR / f"{THREAD}.lock").exists()
    assert is_held(tmp_path, THREAD)
    held.release()
    assert not held.held
    assert not (tmp_path / lock.LOCK_DIR / f"{THREAD}.lock").exists()
    assert not is_held(tmp_path, THREAD)


def test_a_second_writer_is_refused(tmp_path: Path):
    with WriterLock(tmp_path, THREAD) as first:
        assert first.acquire()
        assert not WriterLock(tmp_path, THREAD).acquire()
    assert WriterLock(tmp_path, THREAD).acquire()


def test_acquire_twice_keeps_the_same_lock(tmp_path: Path):
    held = WriterLock(tmp_path, THREAD)
    assert held.acquire()
    assert held.acquire()
    held.release()
    held.release()  # releasing twice is harmless


def test_a_lock_another_process_holds(tmp_path: Path, holder):
    assert is_held(tmp_path, THREAD)
    assert not WriterLock(tmp_path, THREAD).acquire()


def test_a_lock_file_nobody_holds_is_free(tmp_path: Path):
    (tmp_path / lock.LOCK_DIR).mkdir()
    (tmp_path / lock.LOCK_DIR / f"{THREAD}.lock").touch()
    assert not is_held(tmp_path, THREAD)
    assert WriterLock(tmp_path, THREAD).acquire()


def test_no_lock_file_is_free(tmp_path: Path):
    assert not is_held(tmp_path, THREAD)


def test_coordination_is_waited_for_then_given_up(tmp_path: Path):
    directory = tmp_path / lock.LOCK_DIR
    directory.mkdir()
    other = os.open(directory / lock.COORDINATION, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError), lock._coordination(directory, wait=0.2):
            pass
    finally:
        os.close(other)


def test_release_gives_up_the_lock_even_when_coordination_times_out(tmp_path: Path, monkeypatch):
    held = WriterLock(tmp_path, THREAD)
    assert held.acquire()
    directory = tmp_path / lock.LOCK_DIR
    other = os.open(directory / lock.COORDINATION, os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)  # a Codex suspended while holding coordination
    real = lock._coordination
    monkeypatch.setattr(lock, "_coordination", lambda d, wait=0.2: real(d, wait=0.2))
    try:
        with pytest.raises(TimeoutError):
            held.release()
    finally:
        os.close(other)
    assert not held.held
    assert WriterLock(tmp_path, THREAD).acquire()  # the flock was given up


@pytest.mark.parametrize("bad", ["*", "../../outside/x", "not-a-uuid"])
def test_a_malformed_thread_id_names_no_lock_file(tmp_path: Path, bad: str):
    home = tmp_path / "codex"
    home.mkdir()
    with pytest.raises(ValueError):
        WriterLock(home, bad)
    with pytest.raises(ValueError):
        is_held(home, bad)
    assert list(tmp_path.rglob("*.lock")) == []  # no lock file, inside the home or outside it
