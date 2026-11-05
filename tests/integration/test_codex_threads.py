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
from duologue.jsonl import encode as encode_records
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


def test_a_turn_left_open_by_a_stopped_codex_waits_for_the_next_turn(codex, tmp_path):
    # Codex 0.166 does not close such a turn on resume; its next turn closes it.
    path = write_rollout(codex.home, THREAD, codex_turn("turn-1")[:2])
    size = path.stat().st_size
    with pytest.raises(Wait, match="never finished"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert path.stat().st_size == size


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


def test_a_legacy_thread_open_elsewhere_is_not_converted(codex, tmp_path, holder):
    path = write_rollout(codex.home, THREAD, codex_turn("turn-1"), history_mode="legacy")
    assert codex.migrate(THREAD) == "skipped_busy"
    assert '"history_mode":"legacy"' in path.read_text().splitlines()[0]


def test_a_missing_thread_blocks(codex, tmp_path):
    with pytest.raises(Blocked, match="no rollout file"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


def test_a_compressed_thread_waits(codex, tmp_path):
    folder = codex.home / "sessions" / "2026" / "11" / "04"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-11-04T14-00-00-{THREAD}.jsonl.zst").write_bytes(b"")
    with pytest.raises(Wait, match="compressed"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


class CheckingJournal:
    """Checks that the intent is recorded under the writer lock and before any byte is written."""

    def __init__(self, codex: Codex, thread_id: str):
        self.codex, self.thread_id, self.intent = codex, thread_id, None

    def begin(self, intent: Intent) -> None:
        assert lock.is_held(self.codex.home, self.thread_id), "the write must hold the lock"
        size = intent.path.stat().st_size if intent.path.exists() else 0
        assert size == intent.base, "the intent must be recorded before the write"
        self.intent = intent


def test_the_append_holds_the_lock_and_records_its_intent_first(codex, thread, tmp_path):
    journal = CheckingJournal(codex, THREAD)
    append_turns(codex, THREAD, claude_lines(tmp_path), journal=journal)
    assert journal.intent is not None


def test_create_holds_the_lock_and_records_its_intent_first(codex, tmp_path):
    journal = CheckingJournal(codex, "")

    def build(thread_id):
        journal.thread_id = thread_id
        return claude_lines(tmp_path, thread_id=thread_id)

    create_thread(codex, cwd=CWD, build=build, journal=journal)
    assert journal.intent is not None


def two_turns(tmp_path: Path, thread_id: str = THREAD):
    c = ClaudeTranscript()
    c.user("first")
    c.assistant("one")
    c.user("second")
    c.assistant("two")
    return claude_to_codex(
        c.records,
        thread_id=thread_id,
        key="pair-1",
        outputs=OutputStore(tmp_path / "outputs"),
        default_cwd=CWD,
    )


def test_a_partly_indexed_write_blocks_and_stays(codex, thread, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_INDEX_TURNS", "1")
    before = thread.stat().st_size
    with pytest.raises(Blocked, match="indexed 1 of the 2"):
        append_turns(codex, THREAD, two_turns(tmp_path), journal=MemoryJournal())
    assert thread.stat().st_size > before  # Codex indexed part of it: nothing is taken out


def test_a_write_whose_last_line_codex_did_not_index_blocks(codex, thread, tmp_path, monkeypatch):
    # Every turn id is in the index, but the turn's end and the checkpoint are not.
    monkeypatch.setenv("FAKE_CODEX_LEAVE_LINES", "1")
    with pytest.raises(Blocked, match="indexed 0 of the 1 turns duologue added whole"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


def test_a_batch_that_repeats_a_turn_id_blocks(codex, thread, tmp_path):
    lines = claude_lines(tmp_path)
    with pytest.raises(Blocked, match="repeat a turn id"):
        append_turns(codex, THREAD, lines + lines, journal=MemoryJournal())


def test_the_index_is_read_where_codex_keeps_its_databases(codex, tmp_path, monkeypatch):
    elsewhere = tmp_path / "sqlite"
    elsewhere.mkdir()
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(elsewhere))
    write_rollout(codex.home, THREAD, codex_turn("turn-1"))
    resume(codex, THREAD)  # the fake follows CODEX_SQLITE_HOME as Codex does
    assert (elsewhere / "thread_history_1.sqlite").exists()
    assert list(codex.index(THREAD).turns) == ["turn-1"]


def test_a_thread_ending_in_a_half_line_waits(codex, thread, tmp_path):
    with open(thread, "ab") as fh:
        fh.write(b'{"timestamp":"2026-11-04T14:00:09.000Z","type":"event_msg","pay')
    size = thread.stat().st_size
    with pytest.raises(Wait, match="writing"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert thread.stat().st_size == size


def test_an_index_that_does_not_catch_up_blocks(codex, thread, tmp_path, monkeypatch):
    with open(thread, "a") as fh:
        for n, r in enumerate(codex_turn("turn-2"), start=5):
            fh.write(json.dumps(dict(r, ordinal=n), separators=(",", ":")) + "\n")
    size = thread.stat().st_size
    monkeypatch.setenv("FAKE_CODEX_NO_INDEX", "1")
    with pytest.raises(Blocked, match="index of the thread ends at byte"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
    assert thread.stat().st_size == size


def test_resume_of_a_thread_open_elsewhere_waits(codex, thread, holder):
    with pytest.raises(Wait, match="open in Codex"):
        resume(codex, THREAD)


def test_an_archived_thread_waits(codex, thread, tmp_path):
    archived = codex.home / "archived_sessions" / thread.name
    archived.parent.mkdir()
    thread.rename(archived)
    with pytest.raises(Wait, match="archived"):
        append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())


def test_a_retry_reports_where_the_turns_are(codex, thread, tmp_path):
    lines = claude_lines(tmp_path)
    first = append_turns(codex, THREAD, lines, journal=MemoryJournal())
    with open(thread, "a") as fh:  # Codex writes a turn of its own after them
        for n, r in enumerate(codex_turn("turn-9"), start=100):
            fh.write(json.dumps(dict(r, ordinal=n), separators=(",", ":")) + "\n")
    resume(codex, THREAD)
    again = append_turns(codex, THREAD, lines, journal=MemoryJournal())
    assert not again.written
    assert (again.start, again.end) == (first.start, first.end)


def test_a_lone_surrogate_in_claude_output_is_written_as_a_replacement(codex, thread, tmp_path):
    c = ClaudeTranscript()
    c.user("read it")
    c.call("Read", {"file_path": "/x"}, json.loads('"cut emoji \\ud83d"'))
    c.assistant("done")
    lines = claude_to_codex(
        c.records, thread_id=THREAD, key="k", outputs=OutputStore(tmp_path / "o"), default_cwd=CWD
    )
    done = append_turns(codex, THREAD, lines, journal=MemoryJournal())
    assert "cut emoji \ufffd" in thread.read_text(encoding="utf-8")[done.start :]


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


def test_recover_never_takes_out_what_codex_wrote_after(codex, thread, tmp_path, monkeypatch):
    intent = interrupted(codex, thread, tmp_path)
    with open(thread, "ab") as fh:  # the user's own Codex turn, after duologue's lines
        fh.write(encode_records([{"type": "event_msg", "payload": {"type": "user_message"}}]))
    size = thread.stat().st_size
    monkeypatch.setenv("FAKE_CODEX_NO_INDEX", "1")
    with pytest.raises(Blocked, match="left as it is"):
        recover(codex, THREAD, intent)
    assert thread.stat().st_size == size


def test_recover_a_cut_off_write_codex_indexed_part_of_blocks(codex, thread, tmp_path):
    lines = two_turns(tmp_path)
    data = encode(lines, codex.index(THREAD).next_ordinal)
    base = thread.stat().st_size
    first_turn_end = data.index(b'"task_complete"')
    cut = data.index(b"\n", first_turn_end) + 1 + 40  # turn 1 whole, turn 2 cut off
    with open(thread, "ab") as fh:
        fh.write(data[:cut])
    intent = Intent("codex", thread, base, data, turn_ids(lines))
    resume(codex, THREAD)  # the user opened the thread: Codex indexed turn 1
    assert codex.index(THREAD).next_offset > base
    with pytest.raises(Blocked, match="indexed part"):
        recover(codex, THREAD, intent)
    assert thread.stat().st_size == base + cut


def test_recover_a_create_whose_file_was_never_made(codex, tmp_path):
    intent = Intent("codex", tmp_path / "x.jsonl", 0, b"{}\n", ("t",))
    assert not recover(codex, "01a1ab4d-0000-7000-8000-000000000001", intent)


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
    calls = [json.loads(x)["method"] for x in log.read_text().splitlines()]
    return [m for m in calls if m != "initialize"]


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

    def test_a_thread_with_a_turn_running_in_the_daemon_is_not_closed(
        self, codex, tmp_path, daemon
    ):
        path = write_rollout(codex.home, THREAD, codex_turn("turn-1") + codex_turn("turn-2")[:2])
        with codex.app_server() as app:
            app.request("thread/resume", {"threadId": THREAD})
        size = path.stat().st_size
        with pytest.raises(Wait, match="turn is running"):
            append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
        assert "thread/archive" not in methods(daemon)
        assert path.stat().st_size == size

    def test_a_thread_the_user_has_open_stays_open_after_a_resume(self, codex, daemon):
        write_rollout(codex.home, THREAD, codex_turn("turn-1"))
        with codex.app_server() as app:
            app.request("thread/resume", {"threadId": THREAD})
        resume(codex, THREAD)
        assert "thread/archive" not in methods(daemon)
        assert lock.is_held(codex.home, THREAD)

    def test_a_thread_another_process_holds_is_not_closed_by_the_daemon(
        self, codex, thread, tmp_path, daemon, holder
    ):
        with pytest.raises(Wait, match="open in Codex"):
            append_turns(codex, THREAD, claude_lines(tmp_path), journal=MemoryJournal())
        assert "thread/archive" not in methods(daemon)
