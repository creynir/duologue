import json

import pytest
from builders import CWD, ClaudeTranscript

from duologue.translate.claude_to_codex import (
    RESULT_HEADER,
    claude_to_codex,
    is_user_input,
)
from duologue.translate.common import MARK
from duologue.translate.outputs import OutputStore


@pytest.fixture
def store(tmp_path):
    return OutputStore(tmp_path / "outputs")


def translate(records, store, key="pair-1"):
    return claude_to_codex(
        records, thread_id="th-1", key=key, outputs=store, default_cwd="/fallback"
    )


def items(lines):
    return [ln.payload["item"] for ln in lines if ln.payload.get("type") == "item_completed"]


def model_notes(lines):
    return [
        ln.payload["content"][0]["text"]
        for ln in lines
        if ln.type == "response_item" and ln.payload.get("role") == "assistant"
    ]


def events(lines, kind):
    return [ln.payload for ln in lines if ln.payload.get("type") == kind]


def one_call(name, inp, result, error=False):
    t = ClaudeTranscript()
    t.user("do it")
    t.call(name, inp, result, error)
    t.assistant("Done.")
    return t.records


# ------------------------------------------------------------------------------------------ turns
def test_a_typed_message_opens_a_turn_and_its_last_text_is_the_final_answer(store):
    t = ClaudeTranscript()
    t.user("What is in the folder?")
    t.assistant("Looking.")
    t.assistant("Two files.")
    lines = translate(t.records, store)

    started, completed = events(lines, "task_started"), events(lines, "task_complete")
    assert len(started) == len(completed) == 1
    assert completed[0]["turn_id"] == started[0]["turn_id"]
    assert completed[0]["last_agent_message"] == "Two files."
    user = [i for i in items(lines) if i["type"] == "UserMessage"]
    assert user[0]["content"][0]["text"] == "What is in the folder?"
    agent = [i for i in items(lines) if i["type"] == "AgentMessage"]
    assert [a["phase"] for a in agent] == ["commentary", "final_answer"]
    final_notes = [
        ln.payload
        for ln in lines
        if ln.type == "response_item" and ln.payload.get("phase") == "final_answer"
    ]
    assert [n["content"][0]["text"] for n in final_notes] == ["Two files."]


def test_each_typed_message_is_its_own_turn(store):
    t = ClaudeTranscript()
    t.user("first")
    t.assistant("one")
    t.user("second")
    t.assistant("two")
    lines = translate(t.records, store)

    started = [s["turn_id"] for s in events(lines, "task_started")]
    assert len(set(started)) == 2
    assert [c["turn_id"] for c in events(lines, "task_complete")] == started


def test_a_batch_that_starts_mid_answer_opens_with_a_note(store):
    t = ClaudeTranscript()
    t.user("start")
    t.assistant("part one")
    lines = translate(t.records[1:], store)

    assert len(events(lines, "task_started")) == 1
    first = [i for i in items(lines) if i["type"] == "AgentMessage"][0]
    assert first["content"][0]["text"] == f"[{MARK}: continued in Claude]"


# ------------------------------------------------------------------------------------------ tools
def test_bash_becomes_a_command_execution_in_the_records_folder(store):
    lines = translate(one_call("Bash", {"command": "ls -la"}, "a\nb"), store)

    (cmd,) = [i for i in items(lines) if i["type"] == "CommandExecution"]
    assert cmd["command"] == ["/bin/bash", "-lc", "ls -la"]
    assert cmd["cwd"] == "file://" + CWD
    assert cmd["parsed_cmd"] == [{"type": "unknown", "cmd": "ls -la"}]
    assert (cmd["status"], cmd["exit_code"], cmd["aggregated_output"]) == ("completed", 0, "a\nb")


def test_a_failed_tool_shows_as_failed(store):
    lines = translate(one_call("Bash", {"command": "false"}, "Exit code 1", error=True), store)

    (cmd,) = [i for i in items(lines) if i["type"] == "CommandExecution"]
    assert (cmd["status"], cmd["exit_code"]) == ("failed", 1)
    assert "(an error)" in model_notes(lines)[0]


@pytest.mark.parametrize(
    ("name", "inp", "parsed"),
    [
        ("Read", {"file_path": "/work/project/a.py"}, {"type": "read", "name": "a.py"}),
        ("Grep", {"pattern": "TODO", "path": "src"}, {"type": "search", "query": "TODO"}),
        ("Glob", {"pattern": "*.py"}, {"type": "list_files", "path": CWD}),
    ],
)
def test_read_grep_and_glob_become_their_codex_actions(store, name, inp, parsed):
    lines = translate(one_call(name, inp, "result"), store)

    (cmd,) = [i for i in items(lines) if i["type"] == "CommandExecution"]
    assert parsed.items() <= cmd["parsed_cmd"][0].items()


def test_edit_becomes_a_file_change_with_a_diff(store):
    inp = {"file_path": "/work/project/a.py", "old_string": "x = 1\n", "new_string": "x = 2\n"}
    lines = translate(one_call("Edit", inp, "The file has been updated."), store)

    (change,) = [i for i in items(lines) if i["type"] == "FileChange"]
    diff = change["changes"]["/work/project/a.py"]
    assert diff["type"] == "update"
    assert "-x = 1" in diff["unified_diff"] and "+x = 2" in diff["unified_diff"]
    assert change["status"] == "completed"


def test_multiedit_puts_every_edit_in_one_diff(store):
    edits = [{"old_string": "a\n", "new_string": "b\n"}, {"old_string": "c\n", "new_string": "d\n"}]
    lines = translate(one_call("MultiEdit", {"file_path": "/f.txt", "edits": edits}, "ok"), store)

    (change,) = [i for i in items(lines) if i["type"] == "FileChange"]
    diff = change["changes"]["/f.txt"]["unified_diff"]
    assert all(s in diff for s in ("-a", "+b", "-c", "+d"))


def test_write_becomes_an_added_file(store):
    inp = {"file_path": "/work/project/new.md", "content": "hello\n"}
    lines = translate(one_call("Write", inp, "File created successfully"), store)

    (change,) = [i for i in items(lines) if i["type"] == "FileChange"]
    assert change["changes"]["/work/project/new.md"] == {"type": "add", "content": "hello\n"}


def test_mcp_tools_keep_their_server_and_other_tools_go_to_a_claude_server(store):
    t = ClaudeTranscript()
    t.user("look it up")
    t.call("mcp__tracker__get_issue", {"id": "ABC-1"}, [{"type": "text", "text": "{}"}])
    t.call("Agent", {"prompt": "find big files"}, "biggest: x")
    lines = translate(t.records, store)

    calls = [(i["server"], i["tool"]) for i in items(lines) if i["type"] == "McpToolCall"]
    assert calls == [("tracker", "get_issue"), ("claude", "Agent")]


def test_tool_search_is_left_out(store):
    lines = translate(one_call("ToolSearch", {"query": "select:Read"}, "loaded"), store)

    assert not [i for i in items(lines) if i["type"] not in ("UserMessage", "AgentMessage")]
    assert not [n for n in model_notes(lines) if "ToolSearch" in n]


def test_the_model_reads_each_tool_call_as_a_marked_note(store):
    lines = translate(one_call("Bash", {"command": "ls"}, "a"), store)

    assert model_notes(lines)[0] == f"[Claude ran Bash] ls\n{RESULT_HEADER}\na"


def test_a_long_output_is_capped_with_a_pointer_to_the_whole_of_it(store):
    long = "line\n" * 6000  # 30,000 characters
    lines = translate(one_call("Read", {"file_path": "/big.txt"}, long), store)

    (saved,) = list(store.directory.iterdir())
    assert saved.read_text() == long
    pointer = f"[30000 characters; the full output is in {saved}; the start of it:]"
    (cmd,) = [i for i in items(lines) if i["type"] == "CommandExecution"]
    assert cmd["aggregated_output"] == f"{pointer}\n{long[: store.ui_limit]}"  # what the app shows
    assert model_notes(lines)[0].endswith(f"{pointer}\n{long[: store.model_limit]}")  # the model


# ------------------------------------------------------------------------------------- invariants
def test_model_visible_items_carry_no_id(store):
    lines = translate(one_call("Bash", {"command": "ls"}, "a"), store)

    assert all("id" not in ln.payload for ln in lines if ln.type == "response_item")


def test_the_same_records_always_translate_to_the_same_lines(store, tmp_path):
    records = one_call("Edit", {"file_path": "/f", "old_string": "a", "new_string": "b"}, "ok")
    first = translate(records, store)
    again = translate(json.loads(json.dumps(records)), OutputStore(tmp_path / "other"))

    assert [(ln.type, ln.ms, ln.payload) for ln in first] == [
        (ln.type, ln.ms, ln.payload) for ln in again
    ]


def test_ids_depend_on_the_pair(store):
    records = one_call("Bash", {"command": "ls"}, "a")
    a = events(translate(records, store, key="pair-a"), "task_started")[0]["turn_id"]
    b = events(translate(records, store, key="pair-b"), "task_started")[0]["turn_id"]

    assert a != b


def test_what_is_not_conversation_is_left_out(store):
    t = ClaudeTranscript()
    t.user("real question")
    t.assistant(
        [
            {"type": "thinking", "thinking": "hmm", "signature": "s"},
            {"type": "text", "text": "Answer."},
        ]
    )
    t.user("side work", isSidechain=True)
    t.user("<command-name>/clear</command-name>", isMeta=True)
    t.user("This session is being continued from a previous conversation", isCompactSummary=True)
    t.assistant(f"[{MARK}: continued in Codex]")
    t.assistant([{"type": "fallback", "text": "x"}])
    records = t.records + [{"type": "api-request-blob", "uuid": "x", "message": {"content": "dup"}}]
    lines = translate(records, store)

    texts = [i["content"][0]["text"] for i in items(lines)]
    assert texts == ["real question", "Answer."]


@pytest.mark.parametrize(
    ("content", "extra", "expected"),
    [
        ("hello", {}, True),
        ([{"type": "text", "text": "hello"}], {}, True),
        ([{"type": "tool_result", "tool_use_id": "t", "content": "x"}], {}, False),
        ("   ", {}, False),
        (f"[{MARK}: note]", {}, False),
        ("hello", {"isMeta": True}, False),
        ("hello", {"isSidechain": True}, False),
        ("summary", {"isCompactSummary": True}, False),
    ],
)
def test_is_user_input(content, extra, expected):
    record = {"type": "user", "uuid": "u", "message": {"role": "user", "content": content}, **extra}
    assert is_user_input(record) is expected
