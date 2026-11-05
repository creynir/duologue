import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from builders import CWD, SESSION, ClaudeTranscript

from duologue.claude import live
from duologue.claude.sessions import append_records, check_structure, create_session, recover
from duologue.claude.transcript import (
    find_transcript,
    new_transcript_path,
    project_folder,
    settled,
    tail,
)
from duologue.journal import Blocked, Intent, MemoryJournal, Wait
from duologue.jsonl import encode, read_lines
from duologue.translate.codex_to_claude import NOT_RECORDED, ClaudeStep, claude_records
from duologue.translate.common import MARK


def write(tmp_path: Path, records: list[dict]) -> Path:
    path = new_transcript_path(tmp_path, CWD, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(records))
    return path


def a_session() -> ClaudeTranscript:
    c = ClaudeTranscript()
    c.user("run the tests")
    c.call("Bash", {"command": "pytest -q"}, "3 passed")
    c.assistant("All three pass.")
    return c


def codex_steps() -> list[ClaudeStep]:
    return [
        ClaudeStep("user", [{"type": "text", "text": "now lint"}], None, "k:u"),
        ClaudeStep(
            "assistant",
            [{"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {"command": "ruff"}}],
            None,
            "k:call",
        ),
        ClaudeStep(
            "user",
            [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok", "is_error": False}],
            None,
            "k:result",
        ),
        ClaudeStep("assistant", [{"type": "text", "text": "Clean."}], None, "k:a"),
    ]


def records_after(lines, steps=None) -> list[dict]:
    t = tail(lines)
    return claude_records(
        steps or codex_steps(),
        session_id=SESSION,
        parent_uuid=t.parent_uuid,
        parent_type=t.parent_type,
        model=t.model,
        version=t.version,
        cwd=t.cwd,
        dangling=t.dangling,
    )


# ---------------------------------------------------------------------------------- transcript
def test_project_folder_replaces_everything_but_letters_and_digits():
    assert project_folder("/Users/me/my.project_1") == "-Users-me-my-project-1"


def test_find_transcript_takes_the_newest_copy(tmp_path: Path):
    assert find_transcript(tmp_path, SESSION) is None
    old = write(tmp_path, a_session().records)
    moved = tmp_path / "projects" / "-work-project-worktree" / f"{SESSION}.jsonl"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(old.read_bytes())
    os.utime(old, (time.time() - 60, time.time() - 60))
    assert find_transcript(tmp_path, SESSION) == moved


def test_tail_links_to_the_last_chain_record_and_names_the_last_model(tmp_path: Path):
    c = a_session()
    last_message = c.records[-1]["uuid"]
    attachment = {"type": "attachment", "uuid": "att-1", "parentUuid": last_message}
    bookkeeping = {"type": "last-prompt", "lastPrompt": "x", "sessionId": SESSION}
    lines = read_lines(write(tmp_path, [*c.records, attachment, bookkeeping]))
    t = tail(lines)
    assert t.parent_uuid == "att-1"
    assert t.parent_type == "assistant"
    assert t.model == "claude-test-model"
    assert (t.version, t.cwd) == ("2.1.303", CWD)
    assert t.dangling == ()


def test_tail_of_an_empty_transcript_and_a_synthetic_model(tmp_path: Path):
    assert tail([]).parent_uuid is None and tail([]).model == "<synthetic>"
    c = ClaudeTranscript()
    c.user("hi")
    c.assistant("hello")
    c.records[-1]["message"]["model"] = "<synthetic>"
    assert tail(read_lines(write(tmp_path, c.records))).model == "<synthetic>"


def test_tail_finds_tool_calls_left_without_a_result(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "text", "text": "running"}])
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    assert tail(read_lines(write(tmp_path, c.records))).dangling == ("toolu_1",)


def test_settled(tmp_path: Path):
    c = ClaudeTranscript()
    assert settled([])
    c.user("go")
    assert not settled(read_lines(write(tmp_path, c.records)))
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    c.records[-1]["message"]["stop_reason"] = "tool_use"
    assert not settled(read_lines(write(tmp_path, c.records)))
    c.assistant("done")
    assert settled(read_lines(write(tmp_path, c.records)))


# ---------------------------------------------------------------------------------------- live
def test_a_registered_live_session(tmp_path: Path):
    registry = tmp_path / "sessions"
    registry.mkdir()
    entry = {"sessionId": SESSION, "pid": os.getpid(), "status": "idle"}
    (registry / f"{os.getpid()}.json").write_text(json.dumps(entry))
    (registry / "junk.json").write_text("not json")
    assert live.live_pid(tmp_path, SESSION) == os.getpid()
    assert live.live_pid(tmp_path, "another-session") is None


def test_a_registered_session_whose_process_is_gone(tmp_path: Path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    registry = tmp_path / "sessions"
    registry.mkdir()
    entry = {"sessionId": SESSION, "pid": gone.pid, "status": "busy"}
    (registry / f"{gone.pid}.json").write_text(json.dumps(entry))
    assert live.live_pid(tmp_path, SESSION) is None


@pytest.mark.parametrize("flag", [["--resume", SESSION], [f"--resume={SESSION}"], ["-r", SESSION]])
def test_a_process_that_resumed_the_session(tmp_path: Path, flag):
    p = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()", "claude", *flag],
        stdin=subprocess.PIPE,
    )
    try:
        assert live.live_pid(tmp_path, SESSION) == p.pid
    finally:
        p.stdin.close()
        p.wait(10)
    assert live.live_pid(tmp_path, SESSION) is None


def test_alive():
    assert live.alive(os.getpid())
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert not live.alive(gone.pid)


# ------------------------------------------------------------------------------------- append
def test_append_after_a_finished_turn(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    lines = read_lines(path)
    records = records_after(lines)
    assert check_structure([ln.record for ln in lines], records) == []
    journal = MemoryJournal()
    done = append_records(path, records, journal=journal)
    assert done.written and done.start == lines[-1].end and done.end == path.stat().st_size
    assert [ln.record["uuid"] for ln in read_lines(path, done.start)] == list(done.uuids)
    assert journal.intent.ids == done.uuids
    assert MARK in json.dumps(read_lines(path, done.start)[0].record, ensure_ascii=False)


def test_append_twice_writes_once(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    append_records(path, records, journal=MemoryJournal())
    size = path.stat().st_size
    again = append_records(path, records, journal=MemoryJournal())
    assert not again.written and path.stat().st_size == size


def test_append_nothing(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    assert not append_records(path, [], journal=MemoryJournal()).written


def test_some_records_already_there_blocks(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    append_records(path, records[:1], journal=MemoryJournal())
    with pytest.raises(Blocked, match="1 of the"):
        append_records(path, records, journal=MemoryJournal())


def test_a_transcript_ending_mid_line_waits(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    with open(path, "ab") as fh:
        fh.write(b'{"type": "user"')
    with pytest.raises(Wait, match="still being written"):
        append_records(path, records, journal=MemoryJournal())


def test_records_that_fail_the_check_are_not_written(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    del records[1]["message"]["model"]
    size = path.stat().st_size
    with pytest.raises(Blocked, match="names no model"):
        append_records(path, records, journal=MemoryJournal())
    assert path.stat().st_size == size


def test_dangling_tool_calls_are_answered_first(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    path = write(tmp_path, c.records)
    lines = read_lines(path)
    records = records_after(lines)
    first = records[0]["message"]["content"]
    assert first[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": NOT_RECORDED,
        "is_error": True,
    }
    assert first[1]["text"].startswith(f"[{MARK}:")
    assert check_structure([ln.record for ln in lines], records) == []
    assert append_records(path, records, journal=MemoryJournal()).written


def test_only_an_assistant_message_leaves_calls_unanswered():
    with pytest.raises(ValueError):
        claude_records(
            codex_steps(),
            session_id=SESSION,
            parent_uuid="x",
            parent_type="user",
            model="m",
            version="v",
            cwd=CWD,
            dangling=("toolu_1",),
        )


# ---------------------------------------------------------------------------- structure check
def checked(mutate) -> list[str]:
    existing = a_session().records
    lines = [type("L", (), {"record": r})() for r in existing]
    records = records_after(lines)
    mutate(records)
    return check_structure(existing, records)


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda rs: rs[0].update(parentUuid="elsewhere"), "does not link"),
        (lambda rs: rs[1].update(uuid=rs[0]["uuid"]), "repeated uuid"),
        (lambda rs: rs[0].pop("uuid"), "missing or repeated uuid"),
        (lambda rs: rs[2].update(type="assistant"), "second assistant message"),
        (lambda rs: rs[0].update(type="summary"), "not a message"),
        (
            lambda rs: rs[3]["message"]["content"].insert(0, {"type": "thinking", "thinking": ""}),
            "carries thinking",
        ),
        (lambda rs: rs[2]["message"]["content"].clear(), "does not answer exactly"),
        (
            lambda rs: rs[2]["message"]["content"].insert(0, {"type": "text", "text": "x"}),
            "tool results after other content",
        ),
    ],
)
def test_structure_check_problems(mutate, problem):
    problems = checked(mutate)
    assert any(problem in p for p in problems), problems


def test_structure_check_an_unanswered_last_call():
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    assert check_structure([], c.records) == ["the last tool call has no result"]


def test_structure_check_a_call_answered_too_late():
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    existing = c.records[:]
    c.assistant("moving on")
    problems = check_structure(existing, c.records[2:])
    assert any("not answered" in p for p in problems)


# ----------------------------------------------------------------------------------- recovery
def test_recover(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    base = path.stat().st_size
    intent = Intent("claude", path, base, encode(records), tuple(r["uuid"] for r in records))
    assert not recover(path, intent)  # nothing written
    with open(path, "ab") as fh:
        fh.write(intent.data[:40])
    assert not recover(path, intent)  # cut off: taken out
    assert path.stat().st_size == base
    with open(path, "ab") as fh:
        fh.write(intent.data)
    assert recover(path, intent)
    with open(path, "r+b") as fh:
        fh.seek(base)
        fh.write(b"X")
    with pytest.raises(Blocked):
        recover(path, intent)


# ------------------------------------------------------------------------------------- create
def test_create_session(tmp_path: Path):
    journal = MemoryJournal()

    def build(session_id: str) -> list[dict]:
        return claude_records(
            codex_steps(),
            session_id=session_id,
            parent_uuid=None,
            parent_type=None,
            model="<synthetic>",
            version="2.1.318",
            cwd=CWD,
        )

    session_id, done = create_session(
        tmp_path, cwd=CWD, build=build, journal=journal, title="Lint pass"
    )
    assert done.path == tmp_path / "projects" / project_folder(CWD) / f"{session_id}.jsonl"
    assert find_transcript(tmp_path, session_id) == done.path
    records = [ln.record for ln in read_lines(done.path)]
    assert records[-1] == {
        "type": "custom-title",
        "customTitle": "Lint pass",
        "sessionId": session_id,
    }
    assert all(r["sessionId"] == session_id for r in records)
    assert records[0]["parentUuid"] is None
    assert oct(done.path.stat().st_mode & 0o777) == "0o600"
    assert journal.intent.base == 0
