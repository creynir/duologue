"""The Codex append path against the fake codex (tests/fakes/fake_codex.py)."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from builders import CWD, ClaudeTranscript, CodexRollout
from conftest import write_rollout

from duologue.codex import lock
from duologue.codex.threads import (
    Codex,
    append_turns,
    create_thread,
    encode,
    recover,
    resume,
    turn_ids,
)
from duologue.journal import Blocked, Intent, MemoryJournal, Wait
from duologue.jsonl import read_lines
from duologue.translate.claude_to_codex import claude_to_codex
from duologue.translate.outputs import OutputStore

pytestmark = pytest.mark.integration

THREAD = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f96ef"

HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


def codex_turn(turn_id: str, text: str = "hello") -> list[dict]:
    x = CodexRollout(THREAD)
    x.start(turn_id)
    x.user_says(text, f"{turn_id}-u")
    x.agent_says("hi", f"{turn_id}-a")
    x.complete("hi")
    return x.records


def claude_lines(tmp_path: Path, thread_id: str = THREAD, prompt: str = "run the tests"):
    c = ClaudeTranscript()
    c.user(prompt)
    c.call("Bash", {"command": "pytest -q"}, "3 passed")
    c.assistant("All three pass.")
    return claude_to_codex(
        c.records,
        thread_id=thread_id,
        key="pair-1",
        outputs=OutputStore(tmp_path / "outputs"),
        default_cwd=CWD,
    )


@pytest.fixture
def thread(codex: Codex) -> Path:
    """A paginated thread with one Codex turn, indexed by Codex."""
    path = write_rollout(codex.home, THREAD, codex_turn("turn-1"))
    resume(codex, THREAD)
    assert codex.index(THREAD).next_offset == path.stat().st_size
    return path


@pytest.fixture
def holder(codex: Codex):
    """Another process holding the thread's writer lock, as a Codex app with it loaded does."""
    path = codex.home / lock.LOCK_DIR / f"{THREAD}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout.readline().strip() == "held"
    yield p
    p.stdin.close()
    p.wait(10)


def test_append_adds_indexed_turns_with_contiguous_ordinals(codex, thread, tmp_path):
    before = thread.stat().st_size
    lines = claude_lines(tmp_path)
    journal = MemoryJournal()
    done = append_turns(codex, THREAD, lines, journal=journal)
    assert done.written and done.start == before and done.end == thread.stat().st_size
    assert done.turns == turn_ids(lines) and len(done.turns) == 1
    index = codex.index(THREAD)
    assert list(index.turns) == ["turn-1", *done.turns]
    assert index.next_offset == done.end
    ordinals = [ln.record["ordinal"] for ln in read_lines(thread)]
    assert ordinals == list(range(len(ordinals)))
    assert journal.intent.base == before and journal.intent.end == done.end
    assert not (codex.home / lock.LOCK_DIR / f"{THREAD}.lock").exists()


def test_a_retry_writes_nothing(codex, thread, tmp_path):
    lines = claude_lines(tmp_path)
    first = append_turns(codex, THREAD, lines, journal=MemoryJournal())
    size = thread.stat().st_size
    again = append_turns(codex, THREAD, lines, journal=MemoryJournal())
    assert not again.written and again.turns == first.turns
    assert thread.stat().st_size == size


def test_nothing_to_append(codex, thread):
    done = append_turns(codex, THREAD, [], journal=MemoryJournal())
    assert not done.written and done.start == done.end == thread.stat().st_size


def test_a_thread_open_elsewhere_waits(codex, thread, tmp_path, holder):
    size = thread.stat().st_size
    with pytest.raises(Wait, match="open in Codex"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert thread.stat().st_size == size


def test_a_running_turn_waits(codex, tmp_path, holder):
    path = write_rollout(codex.home, THREAD, codex_turn("turn-1")[:2])
    with pytest.raises(Wait, match="turn is running"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert len(read_lines(path)) == 3


def test_a_turn_left_open_by_a_stopped_codex_blocks(codex, tmp_path):
    write_rollout(codex.home, THREAD, codex_turn("turn-1")[:2])
    with pytest.raises(Blocked, match="never finished"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


def test_an_index_that_lags_catches_up_first(codex, thread, tmp_path):
    with open(thread, "a") as fh:  # a turn Codex wrote but has not indexed yet
        for n, r in enumerate(codex_turn("turn-2"), start=5):
            fh.write(json.dumps(dict(r, ordinal=n), separators=(",", ":")) + "\n")
    done = append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert list(codex.index(THREAD).turns) == ["turn-1", "turn-2", *done.turns]


def test_a_write_codex_does_not_index_is_taken_out_again(codex, thread, tmp_path, monkeypatch):
    size = thread.stat().st_size
    monkeypatch.setenv("FAKE_CODEX_NO_INDEX", "1")
    codex = Codex(codex.home, codex.bin, timeout=30)
    with pytest.raises(Blocked, match="taken out of the thread again"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert thread.stat().st_size == size


def test_some_turns_already_there_blocks(codex, thread, tmp_path):
    c = ClaudeTranscript()
    c.user("first")
    c.assistant("one")
    outputs = OutputStore(tmp_path / "outputs")

    def lines():
        return claude_to_codex(
            c.records, thread_id=THREAD, key="pair-1", outputs=outputs, default_cwd=CWD
        )

    append_turns(codex, THREAD, lines(), journal=MemoryJournal())
    c.user("second")
    c.assistant("two")
    with pytest.raises(Blocked, match="1 of the 2 turns"):
        append_turns(codex, THREAD, lines(), journal=MemoryJournal())


def test_a_legacy_thread_is_converted_first(codex, tmp_path):
    path = write_rollout(codex.home, THREAD, codex_turn("turn-1"), history_mode="legacy")
    done = append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert json.loads(path.read_text().splitlines()[0])["payload"]["history_mode"] == "paginated"
    assert list(codex.index(THREAD).turns) == ["turn-1", *done.turns]


def test_a_missing_thread_blocks(codex, tmp_path):
    with pytest.raises(Blocked, match="no rollout file"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


def test_a_compressed_thread_waits(codex, tmp_path):
    folder = codex.home / "sessions" / "2026" / "11" / "04"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-11-04T14-00-00-{THREAD}.jsonl.zst").write_bytes(b"")
    with pytest.raises(Wait, match="compressed"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


# ------------------------------------------------------------------------------------ recovery
def interrupted(codex: Codex, thread: Path, tmp_path: Path, cut: int | None = None) -> Intent:
    """The intent of an append that stopped after writing ``cut`` bytes (all when None)."""
    lines = claude_lines(tmp_path)
    index = codex.index(THREAD)
    data = encode(lines, index.next_ordinal)
    base = thread.stat().st_size
    with open(thread, "ab") as fh:
        fh.write(data if cut is None else data[:cut])
    return Intent("codex", thread, base, data, turn_ids(lines))


def test_recover_a_complete_write_has_codex_index_it(codex, thread, tmp_path):
    intent = interrupted(codex, thread, tmp_path)
    assert recover(codex, THREAD, intent)
    assert all(t in codex.index(THREAD).turns for t in intent.ids)


def test_recover_a_cut_off_write_takes_it_out(codex, thread, tmp_path):
    intent = interrupted(codex, thread, tmp_path, cut=50)
    assert not recover(codex, THREAD, intent)
    assert thread.stat().st_size == intent.base


def test_recover_a_write_that_never_happened(codex, thread, tmp_path):
    intent = interrupted(codex, thread, tmp_path, cut=0)
    assert not recover(codex, THREAD, intent)


def test_recover_an_indexed_write_is_done(codex, thread, tmp_path):
    intent = interrupted(codex, thread, tmp_path)
    resume(codex, THREAD)
    assert recover(codex, THREAD, intent)


def test_recover_a_thread_that_changed_blocks(codex, thread, tmp_path):
    intent = interrupted(codex, thread, tmp_path, cut=0)
    with open(thread, "ab") as fh:
        fh.write(b'{"other":"line"}\n')
    with pytest.raises(Blocked, match="changed after an interrupted write"):
        recover(codex, THREAD, intent)


# -------------------------------------------------------------------------------------- create
def test_create_thread_writes_an_indexed_paginated_thread(codex, tmp_path):
    (codex.home / "config.toml").write_text('model_provider = "local"\n')
    journal = MemoryJournal()
    thread_id, done = create_thread(
        codex,
        cwd=CWD,
        build=lambda tid: claude_lines(tmp_path, thread_id=tid),
        journal=journal,
    )
    first = read_lines(done.path)[0].record
    assert first["type"] == "session_meta" and first["ordinal"] == 0
    meta = first["payload"]
    assert meta["id"] == meta["session_id"] == thread_id
    assert meta["history_mode"] == "paginated"
    assert meta["model_provider"] == "local"
    assert meta["cli_version"] == "0.166.0"
    assert meta["source"] == "vscode"
    assert list(codex.index(thread_id).turns) == list(done.turns)
    assert codex.index(thread_id).next_offset == done.end == done.path.stat().st_size
    assert journal.intent.base == 0


def test_create_thread_that_codex_does_not_index_leaves_no_file(codex, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_NO_INDEX", "1")
    codex = Codex(codex.home, codex.bin, timeout=30)
    journal = MemoryJournal()
    with pytest.raises(Blocked):
        create_thread(
            codex,
            cwd=CWD,
            build=lambda tid: claude_lines(tmp_path, thread_id=tid),
            journal=journal,
        )
    assert not journal.intent.path.exists()


def test_codex_version_and_migrate(codex):
    assert codex.version() == "0.166.0"
    assert codex.migrate(THREAD) == "not_found"
    broken = Codex(codex.home, str(codex.home / "no-such-codex"))
    assert broken.version() is None
    assert broken.migrate(THREAD).startswith("failed:")


# ------------------------------------------------------------------------------- shared daemon
def methods(log: Path) -> list[str]:
    return [json.loads(x)["method"] for x in log.read_text().splitlines()]


class TestSharedDaemon:
    @pytest.fixture
    def codex(self, fake_codex_bin: Path, monkeypatch):
        # A Unix socket path must stay short (104 bytes on macOS): no pytest folder for this home.
        monkeypatch.delenv("FAKE_CODEX_NO_INDEX", raising=False)
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            yield Codex(home=Path(home), bin=str(fake_codex_bin), timeout=30)

    @pytest.fixture
    def daemon(self, codex: Codex, tmp_path: Path, monkeypatch):
        """A shared app-server daemon on the home's control socket, logging every request."""
        log = tmp_path / "requests.jsonl"
        log.touch()
        monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
        p = subprocess.Popen(
            [codex.bin, "app-server", "daemon-serve"],
            env=codex.environment(),
            stdout=subprocess.PIPE,
            text=True,
        )
        assert p.stdout.readline().strip() == "listening"
        yield log
        p.terminate()
        p.wait(10)

    def test_a_thread_loaded_in_the_daemon_is_closed_written_and_given_back(
        self, codex, tmp_path, daemon
    ):
        write_rollout(codex.home, THREAD, codex_turn("turn-1"))
        with codex.app_server() as app:  # an app connected to the daemon opens the thread
            assert app.shared
            app.request("thread/resume", {"threadId": THREAD})
        assert lock.is_held(codex.home, THREAD)
        done = append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
        assert done.written
        assert list(codex.index(THREAD).turns) == ["turn-1", *done.turns]
        assert not lock.is_held(codex.home, THREAD)  # given back: the apps can load it again
        calls = methods(daemon)
        assert calls[calls.index("thread/archive") + 1] == "thread/unarchive"
        assert calls.count("thread/archive") == 2  # closed before the write, unloaded after it

    def test_a_thread_another_process_holds_is_not_closed_by_the_daemon(
        self, codex, thread, tmp_path, daemon, holder
    ):
        with pytest.raises(Wait, match="open in Codex"):
            append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
        assert "thread/archive" not in methods(daemon)
