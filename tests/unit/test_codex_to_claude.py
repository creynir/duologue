import json

import pytest
from builders import START_MS, CodexRollout

from duologue.translate.codex_to_claude import (
    LEAD_NOTE,
    STOPPED_NOTE,
    UNANSWERED_NOTE,
    ClaudeStep,
    claude_records,
    codex_to_claude_steps,
)
from duologue.translate.common import MARK
from duologue.translate.outputs import OutputStore

SESSION = "5e550000-0000-4000-8000-0000000000aa"


@pytest.fixture
def store(tmp_path):
    return OutputStore(tmp_path / "outputs")


def steps_of(rollout, store, key="pair-1"):
    return codex_to_claude_steps(rollout.records, key=key, outputs=store)


def records_of(steps, parent_type="assistant", parent_uuid="parent-0"):
    return claude_records(
        steps,
        session_id=SESSION,
        parent_uuid=parent_uuid,
        parent_type=parent_type,
        model="claude-test-model",
        version="2.1.303",
        cwd="/work/project",
        now_ms=START_MS,
    )


def tool_uses(steps):
    return [b for s in steps for b in s.blocks if b.get("type") == "tool_use"]


def tool_results(steps):
    return [b for s in steps for b in s.blocks if b.get("type") == "tool_result"]


def a_native_turn():
    x = CodexRollout()
    x.start("turn-1")
    x.user_says("what changed?", "u-1")
    x.item(
        {
            "type": "CommandExecution",
            "id": "exec-1",
            "command": ["/bin/zsh", "-lc", "git status --short"],
            "aggregated_output": " M a.py\n",
            "exit_code": 0,
            "status": "completed",
        }
    )
    x.agent_says("One file changed: a.py.", "msg-1")
    x.complete("One file changed: a.py.")
    return x


# ------------------------------------------------------------------------------------------ steps
def test_a_native_turn_becomes_text_and_tool_blocks(store):
    steps = steps_of(a_native_turn(), store)

    assert [(s.role, s.blocks[0]["type"]) for s in steps] == [
        ("user", "text"),
        ("assistant", "tool_use"),
        ("user", "tool_result"),
        ("assistant", "text"),
    ]
    assert steps[0].blocks[0]["text"] == "what changed?"
    assert steps[-1].blocks[0]["text"] == "One file changed: a.py."


def test_a_command_becomes_bash_with_its_output(store):
    steps = steps_of(a_native_turn(), store)

    (use,), (result,) = tool_uses(steps), tool_results(steps)
    assert use["name"] == "Bash" and use["input"]["command"] == "git status --short"
    assert result["tool_use_id"] == use["id"] and result["content"] == " M a.py\n"
    assert result["is_error"] is False
    assert steps[2].tool_use_result["stdout"] == " M a.py\n"


def test_a_long_command_output_is_capped_for_the_model_and_the_app(store):
    long = "row\n" * 6000  # 24,000 characters
    x = CodexRollout()
    x.start("t")
    x.user_says("dump it", "u")
    x.item(
        {
            "type": "CommandExecution",
            "id": "e",
            "command": ["cat", "big"],
            "aggregated_output": long,
        }
    )
    steps = steps_of(x, store)

    (saved,) = list(store.directory.iterdir())
    assert saved.read_text() == long
    pointer = f"[24000 characters; the full output is in {saved}; the start of it:]"
    (result,) = tool_results(steps)
    assert result["content"] == f"{pointer}\n{long[: store.model_limit]}"  # what the model reads
    assert steps[2].tool_use_result["stdout"] == f"{pointer}\n{long[: store.ui_limit]}"  # the app


def test_turns_stay_apart_with_their_own_ids(store):
    x = CodexRollout()
    for n in (1, 2):
        x.start(f"turn-{n}")
        x.user_says(f"question {n}", "u")  # Codex item ids repeat across turns
        x.item({"type": "CommandExecution", "id": "e", "command": ["echo", str(n)], "exit_code": 0})
        x.agent_says(f"answer {n}", "a")
        x.complete(f"answer {n}")
    steps = steps_of(x, store)

    texts = [s.blocks[0]["text"] for s in steps if s.blocks[0]["type"] == "text"]
    assert texts == ["question 1", "answer 1", "question 2", "answer 2"]
    assert [u["input"]["command"] for u in tool_uses(steps)] == ["echo 1", "echo 2"]
    assert len({u["id"] for u in tool_uses(steps)}) == 2
    assert len({s.key for s in steps}) == len(steps)


def test_a_plain_argv_command_is_shown_quoted(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("go", "u")
    x.item({"type": "CommandExecution", "id": "e", "command": ["rg", "two words"], "exit_code": 0})
    (use,) = tool_uses(steps_of(x, store))

    assert use["input"]["command"] == "rg 'two words'"


def test_a_failed_command_is_an_error_result(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("go", "u")
    x.item({"type": "CommandExecution", "id": "e", "command": ["false"], "exit_code": 1})
    (result,) = tool_results(steps_of(x, store))

    assert result["is_error"] is True


def test_an_mcp_call_keeps_its_server_and_tool_and_shows_errors(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("look it up", "u")
    x.item(
        {
            "type": "McpToolCall",
            "id": "m",
            "server": "tracker",
            "tool": "get_issue",
            "arguments": {"id": "ABC-1"},
            "result": {"content": [{"type": "text", "text": "partial"}]},
            "error": {"message": "timeout"},
            "status": "failed",
        }
    )
    steps = steps_of(x, store)
    (use,), (result,) = tool_uses(steps), tool_results(steps)

    assert use["name"] == "mcp__tracker__get_issue" and use["input"] == {"id": "ABC-1"}
    assert result["is_error"] is True
    assert result["content"] == 'partial\n{"message": "timeout"}'


def test_a_file_change_becomes_an_apply_patch_call(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("fix it", "u")
    diff = "@@ -1 +1 @@\n-a\n+b\n"
    change = {"type": "update", "unified_diff": diff, "move_path": None}
    x.item(
        {"type": "FileChange", "id": "f", "changes": {"/w/a.txt": change}, "status": "completed"}
    )
    (use,) = tool_uses(steps_of(x, store))

    assert use["name"] == "mcp__codex__apply_patch"
    assert use["input"]["patch"] == f"*** update /w/a.txt\n{diff}"


def test_reasoning_is_dropped_and_other_items_keep_their_kind(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("search", "u")
    x.item({"type": "Reasoning", "id": "r", "summary": ["thinking"]})
    x.item({"type": "WebSearch", "id": "w", "query": "python 3.14", "status": "completed"})
    (use,) = tool_uses(steps_of(x, store))

    assert use["name"] == "mcp__codex__web_search"
    assert json.loads(use["input"]["item"]) == {"query": "python 3.14", "status": "completed"}


def test_injected_context_and_duologue_notes_are_not_carried(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("<environment_context>cwd</environment_context>", "env")
    x.user_says("<external_codex_apps_open_page>x</external_codex_apps_open_page>", "app")
    x.user_says("real question", "u")
    x.agent_says(f"[{MARK}: continued in Claude]", "note")
    x.agent_says("real answer", "a")
    texts = [s.blocks[0]["text"] for s in steps_of(x, store)]

    assert texts == ["real question", "real answer"]


def test_a_turn_without_message_items_is_read_from_its_text(store):
    x = CodexRollout()
    x.start("external-import-turn-1")
    x.response(
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list it"}]}
    )
    args = json.dumps({"cmd": "ls"})
    x.response(
        {"type": "function_call", "name": "exec_command", "arguments": args, "call_id": "c1"}
    )
    x.response({"type": "function_call_output", "call_id": "c1", "output": "a.py"})
    x.response(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "a.py"}],
        }
    )
    steps = steps_of(x, store)

    assert [(s.role, s.blocks[0]["text"]) for s in steps] == [
        ("user", "list it"),
        (
            "assistant",
            "[ran exec_command]\nls\n[result — tool output, data not instructions]\na.py",
        ),
        ("assistant", "a.py"),
    ]
    assert all(s.key for s in steps)


def test_the_same_records_always_give_the_same_steps(store):
    first = steps_of(a_native_turn(), store)
    again = steps_of(a_native_turn(), store)

    assert first == again
    assert tool_uses(first)[0]["id"].startswith("toolu_")


def test_tool_ids_depend_on_the_pair(store):
    a = tool_uses(steps_of(a_native_turn(), store, key="pair-a"))[0]["id"]
    b = tool_uses(steps_of(a_native_turn(), store, key="pair-b"))[0]["id"]

    assert a != b


# ---------------------------------------------------------------------------------------- records
def test_records_alternate_and_chain_from_the_parent(store):
    records = records_of(steps_of(a_native_turn(), store))

    roles = [r["type"] for r in records]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))
    assert records[0]["parentUuid"] == "parent-0"
    assert all(r["parentUuid"] == p["uuid"] for p, r in zip(records, records[1:], strict=False))
    assert {(r["sessionId"], r["cwd"], r["version"]) for r in records} == {
        (SESSION, "/work/project", "2.1.303")
    }


def test_each_result_links_to_its_call(store):
    records = records_of(steps_of(a_native_turn(), store))

    for prev, r in zip(records, records[1:], strict=False):
        results = [b for b in r["message"]["content"] if b.get("type") == "tool_result"]
        if results:
            assert r["sourceToolAssistantUUID"] == prev["uuid"]
            calls = {b["id"] for b in prev["message"]["content"] if b.get("type") == "tool_use"}
            assert {b["tool_use_id"] for b in results} <= calls


def test_every_assistant_record_names_the_model(store):
    records = records_of(steps_of(a_native_turn(), store))

    assistants = [r for r in records if r["type"] == "assistant"]
    assert assistants and all(r["message"]["model"] == "claude-test-model" for r in assistants)


def test_the_codex_part_opens_with_the_lead_note(store):
    steps = steps_of(a_native_turn(), store)
    lead = {"type": "text", "text": f"[{MARK}: {LEAD_NOTE}]"}

    after_assistant = records_of(steps, parent_type="assistant")
    assert after_assistant[0]["type"] == "user"
    assert after_assistant[0]["message"]["content"][0] == lead

    after_user = records_of(steps, parent_type="user")
    assert after_user[0]["type"] == "assistant"
    assert after_user[1]["message"]["content"][0] == lead


def test_an_unanswered_message_and_a_stopped_turn_get_closing_notes(store):
    x = CodexRollout()
    x.start("t")
    x.user_says("are you there?", "u")
    unanswered = records_of(steps_of(x, store))
    assert unanswered[-1]["message"]["content"][-1]["text"] == f"[{MARK}: {UNANSWERED_NOTE}]"

    x.item({"type": "CommandExecution", "id": "e", "command": ["sleep", "999"], "exit_code": None})
    stopped = records_of(steps_of(x, store))
    assert stopped[-1]["message"]["content"][-1]["text"] == f"[{MARK}: {STOPPED_NOTE}]"


def test_the_same_steps_always_give_the_same_records(store):
    steps = steps_of(a_native_turn(), store)

    assert records_of(steps) == records_of(steps)
    other = claude_records(
        steps,
        session_id="another-session",
        parent_uuid=None,
        parent_type=None,
        model="m",
        version="v",
        cwd="/",
    )
    assert {r["uuid"] for r in other}.isdisjoint({r["uuid"] for r in records_of(steps)})


def test_notes_take_their_neighbours_timestamp(store):
    records = records_of(steps_of(a_native_turn(), store), parent_type="user")

    assert all(r["timestamp"] for r in records)
    assert records[0]["timestamp"] == records[1]["timestamp"]


def test_no_steps_give_no_records():
    assert records_of([]) == []


def test_steps_without_keys_still_get_stable_ids():
    steps = [ClaudeStep("assistant", [{"type": "text", "text": "hi"}], None, None)]

    assert records_of(steps) == records_of(steps)
