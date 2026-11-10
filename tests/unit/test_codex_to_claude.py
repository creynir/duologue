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
    codex_turns,
    parse_unified_diff,
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


def file_change(store, changes, status="completed"):
    x = CodexRollout()
    x.start("t")
    x.user_says("fix it", "u")
    x.item({"type": "FileChange", "id": "f", "changes": changes, "status": status})
    steps = steps_of(x, store)
    (use,), (result,) = tool_uses(steps), tool_results(steps)
    (shown,) = [s.tool_use_result for s in steps if s.tool_use_result]
    return use, result, shown


def test_a_one_hunk_change_becomes_claudes_own_edit(store):
    diff = "@@ -3,3 +3,3 @@\n def f():\n-    return 1\n+    return 2\n \n"
    change = {"type": "update", "unified_diff": diff, "move_path": None}
    use, result, shown = file_change(store, {"/w/a.py": change})

    old, new = "def f():\n    return 1\n", "def f():\n    return 2\n"
    assert use["name"] == "Edit"
    assert use["input"] == {"file_path": "/w/a.py", "old_string": old, "new_string": new}
    assert result["is_error"] is False
    assert shown == {
        "filePath": "/w/a.py",
        "oldString": old,
        "newString": new,
        "originalFile": None,
        "replaceAll": False,
        "structuredPatch": [
            {
                "oldStart": 3,
                "oldLines": 3,
                "newStart": 3,
                "newLines": 3,
                "lines": [" def f():", "-    return 1", "+    return 2", " "],
            }
        ],
        "userModified": False,
    }


def test_a_new_file_becomes_claudes_own_write(store):
    change = {"type": "add", "content": "print('hi')\n"}
    use, _, shown = file_change(store, {"/w/new.py": change})

    assert use["name"] == "Write"
    assert use["input"] == {"file_path": "/w/new.py", "content": "print('hi')\n"}
    assert shown == {
        "type": "create",
        "filePath": "/w/new.py",
        "content": "print('hi')\n",
        "structuredPatch": [],
        "originalFile": None,
        "userModified": False,
    }


def test_a_failed_edit_stays_an_error(store):
    diff = "@@ -1 +1 @@\n-a\n+b\n"
    change = {"type": "update", "unified_diff": diff, "move_path": None}
    use, result, _ = file_change(store, {"/w/a.txt": change}, status="failed")

    assert use["name"] == "Edit" and result["is_error"] is True


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {
                "/w/a.txt": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-a\n+b\n"},
                "/w/b.txt": {"type": "add", "content": "c\n"},
            },
            id="two files",
        ),
        pytest.param(
            {
                "/w/a.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d\n",
                }
            },
            id="two hunks",
        ),
        pytest.param({"/w/a.txt": {"type": "delete", "content": "a\n"}}, id="a delete"),
        pytest.param(
            {
                "/w/a.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
                    "move_path": "/w/b.txt",
                }
            },
            id="a move",
        ),
        pytest.param(
            {"/w/a.txt": {"type": "update", "unified_diff": "@@ -0,0 +1 @@\n+a\n"}},
            id="an insertion with no context",
        ),
        pytest.param(
            {"/w/a.txt": {"type": "update", "unified_diff": "@@ -1,2 +1,2 @@\n-a\n"}},
            id="a cut-off diff",
        ),
        pytest.param(
            {"/w/a.txt": {"type": "update", "unified_diff": "@@ -1,2 +1,2 @@\n-a\n+b\n"}},
            id="a diff one context line short",
        ),
        pytest.param(
            {
                "/w/a.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a\n",
                }
            },
            id="a hunk whose text does not change",
        ),
    ],
)
def test_other_file_changes_stay_one_apply_patch_call(store, changes):
    x = CodexRollout()
    x.start("t")
    x.user_says("fix it", "u")
    x.item({"type": "FileChange", "id": "f", "changes": changes, "status": "completed"})
    steps = steps_of(x, store)
    (use,) = tool_uses(steps)

    assert use["name"] == "mcp__codex__apply_patch"
    for path, change in changes.items():
        assert f"*** {change['type']} {path}\n" in use["input"]["patch"]
    assert not any(s.tool_use_result for s in steps)


def test_a_large_edit_stays_a_patch(tmp_path):
    small = OutputStore(tmp_path / "outputs", model_limit=10)
    change = {"type": "update", "unified_diff": "@@ -1 +1 @@\n-" + "a" * 6 + "\n+" + "b" * 6 + "\n"}
    x = CodexRollout()
    x.start("t")
    x.user_says("fix it", "u")
    x.item({"type": "FileChange", "id": "f", "changes": {"/w/a": change}, "status": "completed"})
    (use,) = tool_uses(steps_of(x, small))

    assert use["name"] == "mcp__codex__apply_patch"


def test_a_large_change_stays_a_patch(tmp_path):
    small = OutputStore(tmp_path / "outputs", model_limit=10)
    change = {"type": "add", "content": "x" * 11}
    x = CodexRollout()
    x.start("t")
    x.user_says("fix it", "u")
    x.item({"type": "FileChange", "id": "f", "changes": {"/w/a": change}, "status": "completed"})
    (use,) = tool_uses(steps_of(x, small))

    assert use["name"] == "mcp__codex__apply_patch"


def test_a_removed_line_that_looks_like_a_header_is_counted_as_removed():
    diff = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- old note\n select 1\n"
    (hunk,) = parse_unified_diff(diff)

    assert hunk["lines"] == ["--- old note", " select 1"]
    assert hunk["oldLines"] == 2 and hunk["newLines"] == 1


def test_a_missing_newline_marker_is_kept_in_the_hunk_but_not_the_strings(store):
    diff = "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    change = {"type": "update", "unified_diff": diff}
    use, _, shown = file_change(store, {"/w/a.txt": change})

    assert use["input"]["old_string"] == "a" and use["input"]["new_string"] == "b"
    assert len(shown["structuredPatch"][0]["lines"]) == 4


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


# ------------------------------------------------------------------------------- rollbacks
def rollback(x: CodexRollout, n: int) -> None:
    x.records.append(
        {
            "timestamp": "2026-11-04T15:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "thread_rolled_back", "num_turns": n},
        }
    )


def a_turn(x: CodexRollout, name: str) -> None:
    x.start(name)
    x.user_says(name, f"{name}-u")
    x.agent_says("ok", f"{name}-a")
    x.complete("ok")


def said(steps) -> list[str]:
    return [s.blocks[0]["text"] for s in steps if s.role == "user"]


@pytest.mark.parametrize(
    "plan, kept, undone",
    [
        (["a", "b", 1, "c"], ["a", "c"], 0),
        (["a", 2, "b"], ["b"], 1),
        ([3, "a"], ["a"], 3),
        (["a", "b", 1, 1, "c"], ["c"], 0),
        (["a", 0, "b"], ["a", "b"], 0),
        (["a", "b", 1], ["a"], 0),
    ],
)
def test_turns_codex_rolled_back_are_left_out_and_counted(store, plan, kept, undone):
    x = CodexRollout()
    for step in plan:
        rollback(x, step) if isinstance(step, int) else a_turn(x, step)

    assert said(steps_of(x, store)) == kept
    assert codex_turns(x.records)[1] == undone


def test_a_rollback_ends_the_turn_it_falls_in(store):
    x = CodexRollout()
    a_turn(x, "a")
    x.start("b")
    x.user_says("b", "b-u")
    rollback(x, 1)  # Codex closes the open turn "b", then drops it
    a_turn(x, "c")

    assert said(steps_of(x, store)) == ["a", "c"]


def test_setup_lines_before_the_first_turn_are_not_a_turn():
    x = CodexRollout()
    x.records.append({"timestamp": "t", "type": "session_meta", "payload": {"id": "th-1"}})
    rollback(x, 1)

    assert codex_turns(x.records)[1] == 1


# ------------------------------------------------------------------------------ echo guard
def test_a_sync_never_sends_back_the_turns_duologue_wrote(store):
    from builders import ClaudeTranscript

    from duologue.translate.claude_to_codex import claude_to_codex

    c = ClaudeTranscript()
    c.user("from claude")
    c.assistant("ok")
    ours = claude_to_codex(
        c.records, thread_id="th-1", key="pair-1", outputs=store, default_cwd="/w"
    )
    x = CodexRollout()
    x.records += [
        {"timestamp": "2026-11-04T14:00:00.000Z", "type": ln.type, "payload": ln.payload}
        for ln in ours
    ]
    a_turn(x, "native")

    assert said(steps_of(x, store)) == ["from claude", "native"]  # a history copy keeps them
    kept = codex_to_claude_steps(x.records, key="pair-1", outputs=store, skip_ours=True)
    assert said(kept) == ["native"]


def test_every_record_names_its_origin_and_the_list_shows_the_last_prompt(store):
    x = CodexRollout()
    a_turn(x, "first")
    x.start("t2")
    x.user_says("second\n  line " + "x" * 300, "t2-u")
    x.agent_says("ok", "t2-a")
    x.complete("ok")
    rs = claude_records(
        steps_of(x, store),
        session_id=SESSION,
        parent_uuid="parent-0",
        parent_type="assistant",
        model="m",
        version="2.1.303",
        cwd="/w",
        now_ms=START_MS,
        last_prompt=True,
    )
    *chain, last = rs

    assert all(r["duologueSource"] == "codex" for r in chain)
    assert last["type"] == "last-prompt" and set(last) == {
        "type",
        "lastPrompt",
        "leafUuid",
        "sessionId",
    }
    assert last["leafUuid"] == chain[-1]["uuid"] and last["sessionId"] == SESSION
    assert last["lastPrompt"].startswith("second line xxx") and last["lastPrompt"].endswith(
        "\u2026"
    )
    assert len(last["lastPrompt"]) == 201


def test_no_last_prompt_without_a_typed_message(store):
    x = CodexRollout()
    x.start("t")
    x.agent_says("on my own", "a")
    x.complete("on my own")
    rs = records_of(steps_of(x, store))

    assert all(r["type"] != "last-prompt" for r in rs)


def test_a_change_result_survives_merging_with_the_next_prompt(store):
    x = CodexRollout()
    x.start("t1")
    x.user_says("make it", "u1")
    x.item(
        {
            "type": "FileChange",
            "id": "f",
            "changes": {"/w/new.py": {"type": "add", "content": "x = 1\n"}},
            "status": "completed",
        }
    )
    x._line("event_msg", {"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"})
    x.start("t2")
    x.user_says("go on", "u2")
    x.agent_says("done", "a2")
    x.complete("done")
    rs = records_of(steps_of(x, store))
    (result,) = [
        r
        for r in rs
        if r["type"] == "user"
        and any(b.get("type") == "tool_result" for b in r["message"]["content"])
    ]

    assert result["toolUseResult"]["type"] == "create"
    assert any(b.get("text") == "go on" for b in result["message"]["content"])


def test_a_form_feed_inside_a_line_does_not_end_the_hunk():
    diff = "@@ -1,3 +1,3 @@\n-a = 1\n+a = 2\n \x0c\n-b = 1\n+b = 2\n"
    (hunk,) = parse_unified_diff(diff)

    assert hunk["lines"] == ["-a = 1", "+a = 2", " \x0c", "-b = 1", "+b = 2"]


def rolled_back(n: int) -> list[dict]:
    payload = {"type": "thread_rolled_back", "num_turns": n}
    return [{"timestamp": "2026-11-04T15:00:00.000Z", "type": "event_msg", "payload": payload}]


def test_undone_turns_are_named_by_their_prompts_or_counted():
    from duologue.translate.codex_to_claude import undone_held

    x = CodexRollout()
    a_turn(x, "first")
    a_turn(x, "second")
    assert undone_held(x.records, rolled_back(1)) == (1, 'the turn(s) that began "second"')
    assert undone_held(x.records, rolled_back(2)) == (
        2,
        'the turn(s) that began "first", "second"',
    )
    x.start("no-prompt")  # a turn with no message from the user
    x.agent_says("on my own", "np-a")
    x.complete("on my own")
    assert undone_held(x.records, rolled_back(2)) == (
        2,
        "the last 2 turn(s) of the conversation Codex held",
    )
    assert undone_held(x.records, rolled_back(5))[0] == 3  # only 3 were there


def test_an_undone_note_from_duologue_is_not_a_turn_claude_holds():
    from duologue.translate.claude_to_codex import note_turn
    from duologue.translate.codex_to_claude import undone_held

    x = CodexRollout()
    a_turn(x, "first")
    note = note_turn("a note for Codex", thread_id="th-1", key="k", ms=START_MS + 99_000)
    records = x.records + [{"type": ln.type, "payload": ln.payload} for ln in note]
    assert undone_held(records, rolled_back(2)) == (1, 'the turn(s) that began "first"')
    assert undone_held(records, rolled_back(1))[0] == 0


def test_turns_codex_imported_from_claude_never_go_back(store):
    """Codex's importer writes a Claude session's own words into turns it names
    external-import-turn-N: a sync pass leaves them out, as it does duologue's turns."""
    x = CodexRollout()
    x.start("external-import-turn-1")
    said = [{"type": "input_text", "text": "said in Claude"}]
    x.response({"type": "message", "role": "user", "content": said})
    answer = [{"type": "output_text", "text": "answered in Claude"}]
    x.response({"type": "message", "role": "assistant", "content": answer})
    x.complete("answered in Claude")
    native = a_native_turn()
    records = x.records + native.records
    kept = codex_to_claude_steps(records, key="k", outputs=store, skip_ours=True)
    assert "said in Claude" not in json.dumps([s.blocks for s in kept])
    assert [k for k, _ in codex_turns(records, skip_ours=True)[0]] == ["turn-1"]
    everything = codex_to_claude_steps(records, key="k", outputs=store)
    assert "said in Claude" in json.dumps([s.blocks for s in everything])


def test_an_undo_of_duologues_note_and_copy_counts_only_the_copy():
    from duologue.translate.claude_to_codex import note_turn
    from duologue.translate.codex_to_claude import undone_held
    from duologue.translate.common import stable_id

    x = CodexRollout()
    a_turn(x, "before")
    before = list(x.records)
    note = note_turn("they happened at the same time", thread_id="th-1", key="k", ms=START_MS)
    copy = CodexRollout()
    copy.start(str(stable_id("claude-turn", "k", "u-1")))  # duologue's copy of a Claude turn
    copy.user_says("in claude", "c-u")
    copy.agent_says("ok", "c-a")
    copy.complete("ok")
    new = [{"type": ln.type, "payload": ln.payload} for ln in note] + copy.records
    assert undone_held(before, new + rolled_back(2)) == (1, 'the turn(s) that began "in claude"')
