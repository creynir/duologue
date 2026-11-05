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
from duologue.jsonl import Line, encode, read_lines
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
        source_assistant=t.last_assistant,
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


def test_tail_matches_parallel_calls_by_id(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}}])
    c.assistant([{"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {}}])
    c.user([{"type": "tool_result", "tool_use_id": "toolu_A", "content": "a"}])
    t = tail(read_lines(write(tmp_path, c.records)))
    assert t.parent_type == "user" and t.dangling == ("toolu_B",)


def test_a_new_prompt_settles_older_calls(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}}])
    c.user("never mind, do this instead")
    c.assistant("ok")
    assert tail(read_lines(write(tmp_path, c.records))).dangling == ()


def test_settled_skips_compaction_and_local_commands(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant("done")
    c.user("This session is being continued from a previous conversation", isCompactSummary=True)
    c.user("<command-name>/cost</command-name>")
    c.user("<local-command-stdout>Total cost: $0.00</local-command-stdout>")
    c.user("caveat", isMeta=True)
    assert settled(read_lines(write(tmp_path, c.records)))


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
WAIT_FOR_INPUT = "import sys; print('ready', flush=True); sys.stdin.read()"


def started(*args: str) -> subprocess.Popen:
    """A process whose command line holds ``args``, once it runs (on Linux a forked child shows
    its parent's command line until it execs)."""
    argv = [sys.executable, "-c", WAIT_FOR_INPUT, *args]
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


def stop(p: subprocess.Popen) -> None:
    p.stdin.close()
    p.wait(10)


def register(home: Path, pid: int, session: str = SESSION) -> None:
    registry = home / "sessions"
    registry.mkdir(exist_ok=True)
    entry = {"sessionId": session, "pid": pid, "status": "idle"}
    (registry / f"{pid}.json").write_text(json.dumps(entry))


def test_a_registered_live_session(tmp_path: Path):
    p = started("claude")
    try:
        register(tmp_path, p.pid)
        (tmp_path / "sessions" / "junk.json").write_text("not json")
        assert live.live_pid(tmp_path, SESSION) == p.pid
        assert live.live_pid(tmp_path, "5e550000-0000-4000-8000-0000000000ff") is None
    finally:
        stop(p)


def test_a_registered_session_whose_process_is_gone(tmp_path: Path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    register(tmp_path, gone.pid)
    assert live.live_pid(tmp_path, SESSION) is None


def test_a_registered_pid_now_used_by_another_program(tmp_path: Path):
    other = subprocess.Popen(["sleep", "30"])
    try:
        register(tmp_path, other.pid)
        assert live.live_pid(tmp_path, SESSION) is None
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize(
    "flag",
    [["--resume", SESSION], [f"--resume={SESSION}"], ["-r", SESSION], ["--session-id", SESSION]],
)
def test_a_process_that_resumed_the_session(tmp_path: Path, flag):
    p = started("claude", *flag)
    try:
        assert live.live_pid(tmp_path, SESSION) == p.pid
    finally:
        stop(p)
    assert live.live_pid(tmp_path, SESSION) is None


def test_a_claude_whose_command_line_mentions_duologue_counts(tmp_path: Path):
    p = started("/home/dev/duologue/bin/claude", "--resume", SESSION)
    try:
        assert live.live_pid(tmp_path, SESSION) == p.pid
    finally:
        stop(p)


def test_naming_the_session_without_resuming_it_does_not_count(tmp_path: Path):
    p = started("claude", "--name", SESSION, "--resume", "other")
    try:
        assert live.live_pid(tmp_path, SESSION) is None
    finally:
        stop(p)


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
    assert check_structure(tail(lines), records) == []
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


def test_a_retry_whose_records_differ_blocks(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    append_records(path, records, journal=MemoryJournal())
    records[-1]["message"]["content"] = [{"type": "text", "text": "something else"}]
    with pytest.raises(Blocked, match="other content"):
        append_records(path, records, journal=MemoryJournal())


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
    assert check_structure(tail(lines), records) == []
    assert append_records(path, records, journal=MemoryJournal()).written


def test_a_closing_result_links_to_the_assistant_message_with_the_call(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    call = c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    attachment = {"type": "attachment", "uuid": "a7700000-0000-4000-8000-000000000001"}
    path = write(tmp_path, [*c.records, {**attachment, "parentUuid": call["uuid"]}])
    records = records_after(read_lines(path))
    assert records[0]["parentUuid"] == attachment["uuid"]
    assert records[0]["sourceToolAssistantUUID"] == call["uuid"]


def test_an_unreadable_process_list_counts_as_open(tmp_path: Path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(live.subprocess, "run", broken)
    assert live.live_pid(tmp_path, SESSION) == live.UNKNOWN
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    with pytest.raises(Wait, match="could not be read"):
        append_records(
            path, records, journal=MemoryJournal(), live=lambda: live.live_pid(tmp_path, SESSION)
        )


def test_a_parallel_call_left_without_result_is_answered_after_the_other_result(tmp_path: Path):
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {}}])
    c.assistant([{"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {}}])
    c.user([{"type": "tool_result", "tool_use_id": "toolu_A", "content": "a"}])
    path = write(tmp_path, c.records)
    lines = read_lines(path)
    records = records_after(lines)
    first = records[0]
    assert first["type"] == "user"
    assert first["message"]["content"][0]["tool_use_id"] == "toolu_B"
    assert check_structure(tail(lines), records) == []
    assert append_records(path, records, journal=MemoryJournal()).written


def test_the_live_check_runs_before_the_write(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    size = path.stat().st_size
    with pytest.raises(Wait, match="process 4242"):
        append_records(path, records, journal=MemoryJournal(), live=lambda: 4242)
    assert path.stat().st_size == size


def test_a_claude_that_opens_the_session_during_the_write_gets_it_back_untouched(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    size = path.stat().st_size
    answers = iter([None, 4242])
    with pytest.raises(Wait, match="opened the session"):
        append_records(path, records, journal=MemoryJournal(), live=lambda: next(answers))
    assert path.stat().st_size == size


def test_a_record_claude_writes_after_duologue_is_never_taken_back(tmp_path: Path, monkeypatch):
    from duologue.claude import sessions

    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    claude_record = b'{"type": "user", "uuid": "c1a0de00-0000-4000-8000-0000000000ee"}\n'
    real = sessions._take_back

    def claude_writes_first(p, intent):
        with open(p, "ab") as fh:  # Claude appends between the live check and the take-back
            fh.write(claude_record)
        return real(p, intent)

    monkeypatch.setattr(sessions, "_take_back", claude_writes_first)
    answers = iter([None, 4242])
    with pytest.raises(Blocked, match="opened the session"):
        append_records(path, records, journal=MemoryJournal(), live=lambda: next(answers))
    assert path.read_bytes().endswith(claude_record)


def test_a_transcript_that_moved_on_waits(tmp_path: Path):
    c = a_session()
    path = write(tmp_path, c.records)
    records = records_after(read_lines(path))
    c.user("one more thing")
    path.write_bytes(encode(c.records))
    with pytest.raises(Wait, match="moved on"):
        append_records(path, records, journal=MemoryJournal())


class CheckingJournal:
    """Records the intent, and checks it is recorded before anything is written."""

    def __init__(self):
        self.intent = None

    def begin(self, intent):
        size = intent.path.stat().st_size if intent.path.exists() else 0
        assert size == intent.base, "the intent must be recorded before the write"
        self.intent = intent


def test_the_intent_is_recorded_before_the_claude_write(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    journal = CheckingJournal()
    append_records(path, records_after(read_lines(path)), journal=journal)
    assert journal.intent is not None


# ---------------------------------------------------------------------------- structure check
def checked(mutate) -> list[str]:
    lines = [Line(r, 0) for r in a_session().records]
    records = records_after(lines)
    mutate(records)
    return check_structure(tail(lines), records)


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda rs: rs[0].update(parentUuid="elsewhere"), "does not link"),
        (lambda rs: rs[1].update(uuid=rs[0]["uuid"]), "repeated uuid"),
        (lambda rs: rs[0].pop("uuid"), "missing, malformed or repeated uuid"),
        (lambda rs: rs[0].update(uuid=12345), "malformed"),
        (lambda rs: rs[0]["message"].update(content={"text": "x"}), "no message content"),
        (
            lambda rs: rs[2]["message"]["content"].append(dict(rs[2]["message"]["content"][0])),
            "once each",
        ),
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
    assert check_structure(tail([]), c.records) == ["the last tool call has no result"]


def test_structure_check_a_call_answered_too_late():
    c = ClaudeTranscript()
    c.user("go")
    c.assistant([{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}])
    existing = [Line(r, 0) for r in c.records]
    c.assistant("moving on")
    problems = check_structure(tail(existing), c.records[2:])
    assert any("not answered" in p for p in problems)


def test_structure_check_two_user_messages_without_owed_results():
    c = ClaudeTranscript()
    c.user("go")
    existing = [Line(r, 0) for r in c.records]
    c.user("and this")
    assert any("second user message" in p for p in check_structure(tail(existing), c.records[1:]))


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


def test_recover_an_interrupted_create(tmp_path: Path):
    path = tmp_path / "new.jsonl"
    intent = Intent("claude", path, 0, b'{"uuid": "x"}\n', ("x",))
    assert not recover(path, intent)  # the file was never made
    path.write_bytes(intent.data[:5])
    assert not recover(path, intent)  # cut off: the empty start is removed
    assert not path.exists()


def test_recover_a_write_the_app_wrote_after(tmp_path: Path):
    path = write(tmp_path, a_session().records)
    records = records_after(read_lines(path))
    base = path.stat().st_size
    intent = Intent("claude", path, base, encode(records), tuple(r["uuid"] for r in records))
    with open(path, "ab") as fh:
        fh.write(intent.data + b'{"type": "last-prompt"}\n')
    assert recover(path, intent)


# ------------------------------------------------------------------------------------- create
def test_create_session_checks_its_records(tmp_path: Path):
    def build(session_id: str) -> list[dict]:
        records = claude_records(
            codex_steps(),
            session_id=session_id,
            parent_uuid=None,
            parent_type=None,
            model="m",
            version="2.1.318",
            cwd=CWD,
        )
        del records[1]["message"]["model"]
        return records

    with pytest.raises(Blocked, match="names no model"):
        create_session(tmp_path, cwd=CWD, build=build, journal=MemoryJournal())
    assert not (tmp_path / "projects").exists() or not any((tmp_path / "projects").rglob("*.jsonl"))


def test_ids_are_checked_before_they_name_a_file(tmp_path: Path):
    with pytest.raises(ValueError):
        find_transcript(tmp_path, "*")
    with pytest.raises(ValueError):
        find_transcript(tmp_path, "../../etc/passwd")


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
