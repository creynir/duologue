"""Syncing pairs end to end against the fake codex and Claude transcripts in throwaway homes."""

import gzip
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from builders import CWD, SESSION, ClaudeTranscript, CodexRollout
from conftest import write_rollout

from duologue import config, gate, linking
from duologue.claude.transcript import find_transcript, project_folder
from duologue.codex.threads import Codex, resume
from duologue.engine import Engine
from duologue.journal import Blocked, Wait
from duologue.jsonl import encode, read_lines
from duologue.pairs import ActivityLog, Store
from duologue.translate.common import MARK

pytestmark = pytest.mark.integration

THREAD = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f96ef"
FAKE_FORMAT = "0970d133aa35b0e78aa8236fad356a80d76bd4a6f705457d0c895b7249dc1ad9"


@pytest.fixture
def homes(tmp_path: Path, fake_codex_bin: Path, monkeypatch):
    duo, claude, codex = tmp_path / "duo", tmp_path / "claude", tmp_path / "codex"
    for d in (duo, claude, codex):
        d.mkdir()
    # These tests pair by hand and stop at a conflict; test_auto covers the defaults.
    (duo / "config.toml").write_text(
        f'codex_bin = "{fake_codex_bin}"\nclaude_bin = "{tmp_path / "no-claude"}"\n'
        'quiet_seconds = 0\nauto = false\non_conflict = "stop"\n'
    )
    monkeypatch.setenv("DUOLOGUE_HOME", str(duo))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    for knob in ("FAKE_CODEX_NO_INDEX", "FAKE_CODEX_SCHEMA", "FAKE_CODEX_INDEX_TURNS"):
        monkeypatch.delenv(knob, raising=False)
    monkeypatch.setattr(gate, "KNOWN_FORMATS", {FAKE_FORMAT: "the fake codex"})
    return SimpleNamespace(duo=duo, claude=claude, codex=codex, bin=str(fake_codex_bin))


def engine() -> Engine:
    cfg = config.load()
    store = Store(cfg.state_file)
    return Engine(cfg, store.load(), store, ActivityLog(cfg.log_file))


class Claude:
    """A Claude session on disk that grows as a test adds turns."""

    def __init__(self, home: Path):
        self.t = ClaudeTranscript()
        self.path = home / "projects" / project_folder(CWD) / f"{SESSION}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def turn(self, prompt: str, answer: str) -> None:
        self.t.user(prompt)
        self.t.call("Bash", {"command": f"echo {prompt}"}, prompt)
        self.t.assistant(answer)
        self.save()

    def save(self) -> None:
        """Append the builder's new records, linked after whatever is last in the file now (it
        may be a record duologue appended), as Claude Code does."""
        there = [ln.record for ln in read_lines(self.path)] if self.path.exists() else []
        known = {r.get("uuid") for r in there}
        new = [r for r in self.t.records if r["uuid"] not in known]
        chain = [r for r in there if r.get("uuid")]  # not a last-prompt line
        if new and chain:
            new[0] = dict(new[0], parentUuid=chain[-1]["uuid"])
        with open(self.path, "ab") as fh:
            fh.write(encode(new))

    def records(self) -> list[dict]:
        return [ln.record for ln in read_lines(self.path)]


class CodexThread:
    """A Codex thread on disk that grows as a test adds turns (indexed, as Codex would)."""

    def __init__(self, home: Path, bin: str):
        self.codex = Codex(home, bin, timeout=30)
        self.n = 0
        self.path = write_rollout(home, THREAD, self._turn("hello", "hi"))
        resume(self.codex, THREAD)

    def _turn(self, prompt: str, answer: str) -> list[dict]:
        self.n += 1
        x = CodexRollout(THREAD, start_ms=1_793_900_000_000 + self.n * 60_000)
        turn = f"codex-turn-{self.n}"
        x.start(turn)
        x.user_says(prompt, f"{turn}-u")
        x.item(
            {
                "type": "CommandExecution",
                "id": f"{turn}-c",
                "command": ["/bin/zsh", "-lc", f"echo {prompt}"],
                "aggregated_output": prompt,
                "exit_code": 0,
                "status": "completed",
            }
        )
        x.agent_says(answer, f"{turn}-a")
        x.complete(answer)
        return x.records

    def turn(self, prompt: str, answer: str, before: tuple[dict, ...] = ()) -> None:
        """A finished turn, after the lines ``before`` (a rollback, say)."""
        self.add([*before, *self._turn(prompt, answer)])

    def add(self, records: list[dict]) -> None:
        ordinal = self.codex.index(THREAD).next_ordinal
        with open(self.path, "a") as fh:
            for k, r in enumerate(records):
                fh.write(json.dumps(dict(r, ordinal=ordinal + k), separators=(",", ":")) + "\n")
        resume(self.codex, THREAD)

    def rollback(self, n: int) -> dict:
        """The line Codex writes when the user edits an earlier message or undoes turns."""
        payload = {"type": "thread_rolled_back", "num_turns": n}
        return {"timestamp": "2026-11-04T15:00:00.000Z", "type": "event_msg", "payload": payload}

    def turn_ids(self) -> list[str]:
        return list(self.codex.index(THREAD).turns)


@pytest.fixture
def pair(homes):
    claude = Claude(homes.claude)
    claude.turn("first", "done in Claude")
    codex = CodexThread(homes.codex, homes.bin)
    e = engine()
    p, said = linking.link(e, claude=SESSION, codex=THREAD, name=None, cwd=CWD)
    assert "from now on" in said
    return SimpleNamespace(claude=claude, codex=codex, name=p.name)


def prompts(records: list[dict]) -> list[str]:
    """The prompts in Claude records: the user's text blocks, without duologue's notes."""
    out = []
    for r in records:
        if r.get("type") != "user":
            continue
        content = r["message"]["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        out += [
            b["text"]
            for b in blocks
            if b.get("type") == "text" and not b["text"].startswith(f"[{MARK}")
        ]
    return out


def sync(name: str, force: bool = True):
    e = engine()
    return e.sync(e.pairs[name], force=force), e.pairs[name]


# ---------------------------------------------------------------------------------- the basics
def test_a_linked_pair_starts_up_to_date(pair):
    outcome, p = sync(pair.name)
    assert outcome.state == "idle"
    assert p.name == "first"  # named after the Claude session's first prompt


def items(path: Path) -> list[dict]:
    return [
        ln.record["payload"]["item"]
        for ln in read_lines(path)
        if ln.record.get("payload", {}).get("type") == "item_completed"
    ]


def test_a_claude_turn_reaches_codex_as_native_items(pair):
    before_turns, before_items = pair.codex.turn_ids(), len(items(pair.codex.path))
    pair.claude.turn("second", "done again")
    outcome, p = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert len(pair.codex.turn_ids()) == len(before_turns) + 1
    added = items(pair.codex.path)[before_items:]
    assert [i["type"] for i in added] == ["UserMessage", "CommandExecution", "AgentMessage"]
    assert added[1]["command"] == ["/bin/bash", "-lc", "echo second"]
    assert p.copied["to_codex"] == 1
    assert sync(pair.name)[0].state == "idle"  # nothing echoes back to Claude


def test_a_codex_turn_reaches_claude_as_tool_calls(pair):
    pair.codex.turn("from codex", "codex answered")
    outcome, p = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    *added, last = pair.claude.records()[-5:]
    texts = json.dumps(added, ensure_ascii=False)
    assert MARK in texts and "from codex" in texts and "codex answered" in texts
    assert any(
        b.get("type") == "tool_use" and b.get("name") == "Bash"
        for r in added
        for b in (r["message"]["content"] if isinstance(r["message"]["content"], list) else [])
    )
    assert all(r["duologueSource"] == "codex" for r in added)
    # Claude's session list shows the last prompt from the end of the file.
    assert last == {
        "type": "last-prompt",
        "lastPrompt": "from codex",
        "leafUuid": added[-1]["uuid"],
        "sessionId": SESSION,
    }
    assert p.copied["to_claude"] == 1
    assert sync(pair.name)[0].state == "idle"  # nothing echoes back to Codex


def test_turns_go_back_and_forth(pair):
    for n in range(2):
        pair.claude.turn(f"claude {n}", "ok")
        assert sync(pair.name)[0].state == "synced"
        pair.codex.turn(f"codex {n}", "ok")
        assert sync(pair.name)[0].state == "synced"
    assert sync(pair.name)[0].state == "idle"
    transcript = json.dumps(pair.claude.records())
    assert "codex 0" in transcript and "codex 1" in transcript


# ------------------------------------------------------------------------------- waiting/busy
@pytest.fixture
def claude_running():
    p = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.read()",
            "claude",
            "--resume",
            SESSION,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout.readline().strip() == "ready"
    yield p
    p.stdin.close()
    p.wait(10)


def test_codex_turns_wait_while_claude_has_the_session_open(pair, claude_running):
    pair.codex.turn("from codex", "ok")
    size = pair.claude.path.stat().st_size
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "open" in outcome.text
    assert pair.claude.path.stat().st_size == size


def test_a_claude_turn_still_running_waits(pair, claude_running):
    pair.claude.t.user("unanswered so far")
    pair.claude.save()
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "working" in outcome.text


def test_a_quiet_pass_skips_unchanged_sides(pair):
    assert sync(pair.name, force=False)[0].state == "idle"
    e = engine()
    p = e.pairs[pair.name]
    assert p.seen  # sizes and times recorded
    assert e.sync(p).text == "up to date"


# ---------------------------------------------------------------------------------- conflicts
def test_writing_in_both_apps_is_a_conflict_until_one_side_is_kept(pair):
    pair.claude.turn("in claude", "ok")
    pair.codex.turn("in codex", "ok")
    claude_size = pair.claude.path.stat().st_size
    turns = pair.codex.turn_ids()
    outcome, p = sync(pair.name)
    assert outcome.state == "conflict" and p.conflict
    assert pair.claude.path.stat().st_size == claude_size and pair.codex.turn_ids() == turns
    assert sync(pair.name)[0].state == "conflict"  # it stays until resolved
    e = engine()
    assert "keeping Claude" in linking.resolve(e, e.pairs[pair.name], "claude")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Claude turn" in outcome.text
    assert "in codex" not in json.dumps(pair.claude.records())  # Codex's turn stays in Codex


def test_resolve_by_keeping_codex(pair):
    pair.claude.turn("in claude", "ok")
    pair.codex.turn("in codex", "ok")
    sync(pair.name)
    e = engine()
    linking.resolve(e, e.pairs[pair.name], "codex")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Codex turn" in outcome.text
    assert "in codex" in json.dumps(pair.claude.records())


# ------------------------------------------------------------------------- rewrites and holds
def test_a_rewritten_claude_transcript_blocks_until_released(pair):
    data = pair.claude.path.read_bytes()
    pair.claude.path.write_bytes(data.replace(b"done in Claude", b"DONE IN CLAUDE"))
    outcome, p = sync(pair.name)
    assert outcome.state == "blocked" and "changed before" in outcome.text
    e = engine()
    said = linking.release(e, e.pairs[pair.name])
    assert "unblocked" in said and "Claude side" in said
    pair.claude.turn("after the rewrite", "ok")
    assert sync(pair.name)[0].state == "synced"


def test_a_codex_rewrite_keeps_the_synced_turn(pair):
    # Codex rewrites a whole thread when it converts it; the synced turn is found again.
    text = pair.codex.path.read_text()
    pair.codex.path.write_text(text.replace('"cwd":"/work/project"', '"cwd":"/work/project/."', 1))
    pair.codex.turn("after", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert "found again" in json.dumps(ActivityLog(engine().config.log_file).tail(5))


def test_hold_and_release(pair):
    e = engine()
    e.pairs[pair.name].hold = "testing"
    e.save()
    pair.claude.turn("held back", "ok")
    assert sync(pair.name)[0].state == "held"
    e = engine()
    assert linking.release(e, e.pairs[pair.name]) == "taken off hold"
    assert sync(pair.name)[0].state == "synced"


def test_an_unknown_claude_record_with_a_message_blocks(pair):
    with open(pair.claude.path, "ab") as fh:
        fh.write(encode([{"type": "assistant_v2", "uuid": "x", "message": {"content": "?"}}]))
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "assistant_v2" in outcome.text


# ----------------------------------------------------------------------------------- recovery
def test_an_append_interrupted_before_codex_indexed_it_is_settled_next_pass(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads.finish
    monkeypatch.setattr(threads, "finish", lambda *a: (_ for _ in ()).throw(OSError("stopped")))
    outcome, p = sync(pair.name)
    assert outcome.state == "error" and p.pending is not None
    monkeypatch.setattr(threads, "finish", real)
    outcome, p = sync(pair.name)
    assert p.pending is None, outcome.text
    assert any("is complete" in x["text"] for x in ActivityLog(engine().config.log_file).tail(5))
    assert sync(pair.name)[0].state == "idle"


# -------------------------------------------------------------------------------- the gate
def test_an_untested_codex_format_waits(pair, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCHEMA", "changed")
    pair.claude.turn("second", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "has not been tested" in outcome.text


def test_allowing_an_untested_codex(pair, monkeypatch, homes):
    monkeypatch.setenv("FAKE_CODEX_SCHEMA", "changed")
    with open(homes.duo / "config.toml", "a") as fh:
        fh.write("allow_untested_codex = true\n")
    pair.claude.turn("second", "ok")
    assert sync(pair.name)[0].state == "synced"


# -------------------------------------------------------------------- linking one side only
def test_link_a_claude_session_creates_its_codex_thread(homes):
    claude = Claude(homes.claude)
    claude.turn("build it", "built")
    claude.turn("test it", "tested")
    e = engine()
    p, said = linking.link(e, claude=SESSION, codex=None, name="the build", cwd=CWD)
    assert "created Codex thread" in said and "2 turn(s)" in said
    codex = Codex(homes.codex, homes.bin)
    assert len(codex.index(p.codex).turns) == 2
    claude.turn("ship it", "shipped")
    outcome, _ = sync("the build")
    assert outcome.state == "synced" and len(codex.index(p.codex).turns) == 3


def test_link_over_the_budget_copies_the_newest_turns_with_a_note(homes):
    claude = Claude(homes.claude)
    for n in range(5):
        claude.turn(f"step {n} " + "x" * 300, "ok")
    with open(homes.duo / "config.toml", "a") as fh:
        fh.write("copy_budget = 1500\n")
    e = engine()
    p, said = linking.link(e, claude=SESSION, codex=None, name="big", cwd=CWD)
    assert "older turn(s) left out" in said
    codex = Codex(homes.codex, homes.bin)
    rollout = codex.rollout(p.codex).read_text()
    assert "earlier turn(s) are not copied here" in rollout and "step 4" in rollout
    assert "step 0" not in rollout


def test_link_a_codex_thread_creates_its_claude_session(homes):
    codex = CodexThread(homes.codex, homes.bin)
    codex.turn("second", "two")
    e = engine()
    p, said = linking.link(e, claude=None, codex=THREAD, name=None, cwd=CWD)
    assert "created Claude session" in said and p.name == "hello"
    path = find_transcript(homes.claude, p.claude)
    records = [ln.record for ln in read_lines(path)]
    assert records[-1] == {"type": "custom-title", "customTitle": "hello", "sessionId": p.claude}
    assert all(
        r["message"]["model"] == "<synthetic>" for r in records if r.get("type") == "assistant"
    )
    codex.turn("third", "three")
    assert sync("hello")[0].state == "synced"


def test_link_refuses_what_is_already_linked_and_bad_ids(pair):
    e = engine()
    with pytest.raises(linking.LinkError, match="already linked"):
        linking.link(e, claude=SESSION, codex=None, name=None, cwd=CWD)
    with pytest.raises(linking.LinkError, match="no Claude Code session"):
        linking.link(e, claude="*", codex=None, name=None, cwd=CWD)
    with pytest.raises(linking.LinkError, match="name a Claude session"):
        linking.link(e, claude=None, codex=None, name=None, cwd=CWD)


def test_link_last_finds_the_newest_conversations_in_the_folder(homes):
    Claude(homes.claude).turn("hi", "hello")
    CodexThread(homes.codex, homes.bin)
    e = engine()
    p, _ = linking.link(e, claude="last", codex="last", name="x", cwd=CWD)
    assert (p.claude, p.codex) == (SESSION, THREAD)


def test_unlink_and_find_pair(pair):
    e = engine()
    assert linking.find_pair(e, "fir").name == pair.name
    assert linking.find_pair(e, THREAD).name == pair.name
    with pytest.raises(linking.LinkError):
        linking.find_pair(e, "nothing")
    assert "unlinked" in linking.unlink(e, e.pairs[pair.name])
    assert engine().pairs == {}


def test_a_missing_side_blocks(pair):
    pair.claude.path.unlink()
    outcome, p = sync(pair.name)
    assert outcome.state == "blocked" and "not on this machine" in outcome.text
    assert p.blocked


# ------------------------------------------------------------- what counts as new content
def test_bookkeeping_records_never_block_or_count_as_content(pair):
    # Claude Code 2.1.318 writes api-request-blob records (a "message", no uuid) and, on every
    # resume, titles and other bookkeeping: none of it is conversation.
    with open(pair.claude.path, "ab") as fh:
        fh.write(
            encode(
                [
                    {"type": "api-request-blob", "hash": "h", "message": {"role": "system"}},
                    {"type": "custom-title", "customTitle": "t", "sessionId": SESSION},
                    {"type": "last-prompt", "lastPrompt": "x", "sessionId": SESSION},
                ]
            )
        )
    pair.codex.turn("from codex", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Codex turn" in outcome.text  # no conflict, no block


def test_local_slash_commands_are_not_prompts(pair):
    pair.claude.t.user("<command-name>/cost</command-name>")
    pair.claude.t.user("<local-command-stdout>Total cost: $0.00</local-command-stdout>")
    pair.claude.save()
    turns = pair.codex.turn_ids()
    assert sync(pair.name)[0].state == "idle"  # nothing for Codex
    assert pair.codex.turn_ids() == turns
    pair.codex.turn("from codex", "ok")
    assert sync(pair.name)[0].state == "synced"  # and no false conflict


# ---------------------------------------------------------------- the unforced timer path
def test_an_unforced_pass_still_notices_new_turns(pair):
    assert sync(pair.name, force=False)[0].state == "idle"
    pair.claude.turn("second", "ok")
    assert sync(pair.name, force=False)[0].state == "synced"
    pair.codex.turn("from codex", "ok")
    assert sync(pair.name, force=False)[0].state == "synced"
    assert sync(pair.name, force=False)[0].state == "idle"


def test_a_finished_claude_turn_waits_for_the_quiet_time(pair, homes):
    config_file = homes.duo / "config.toml"
    config_file.write_text(
        config_file.read_text().replace("quiet_seconds = 0", "quiet_seconds = 3600")
    )
    pair.claude.turn("second", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "just ended" in outcome.text


def test_a_finished_turn_travels_while_claude_stays_open(pair, claude_running):
    pair.claude.turn("second", "ok")  # Claude Code still runs, but its turn has ended
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text


# ------------------------------------------------------------------ only finished turns
def test_only_finished_codex_turns_travel(pair):
    first = pair.codex._turn("finished one", "done")
    second = pair.codex._turn("still running", "later")
    ordinal = pair.codex.codex.index(THREAD).next_ordinal
    with open(pair.codex.path, "a") as fh:
        for k, r in enumerate(first + second[:2]):  # the second turn has started, not ended
            fh.write(json.dumps(dict(r, ordinal=ordinal + k), separators=(",", ":")) + "\n")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn" in outcome.text
    transcript = json.dumps(pair.claude.records())
    assert "finished one" in transcript and "still running" not in transcript
    with open(pair.codex.path, "a") as fh:
        for k, r in enumerate(second[2:]):
            fh.write(json.dumps(dict(r, ordinal=ordinal + 7 + k), separators=(",", ":")) + "\n")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn" in outcome.text
    asked = prompts(pair.claude.records())
    assert asked.count("still running") == 1 and asked.count("finished one") == 1


# ----------------------------------------------------------------- crashes and recovery
class Crash(BaseException):
    """What a killed process looks like to the code: nothing catches it."""


def crash_in(monkeypatch, module, name):
    def boom(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(module, name, boom)


def test_a_crash_before_the_codex_bytes_land_is_undone_then_copied_once(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads._write
    crash_in(monkeypatch, threads, "_write")
    with pytest.raises(Crash):
        sync(pair.name)
    assert engine().pairs[pair.name].pending is not None  # durable before the write
    monkeypatch.setattr(threads, "_write", real)
    before = len(pair.codex.turn_ids())
    outcome, p = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert len(pair.codex.turn_ids()) == before + 1 and p.pending is None
    assert any("was undone" in x["text"] for x in ActivityLog(engine().config.log_file).tail(5))


def test_a_crash_after_the_claude_bytes_land_is_confirmed_and_never_echoed(pair, monkeypatch):
    from duologue.claude import sessions

    pair.codex.turn("from codex", "ok")
    real = sessions._verify
    crash_in(monkeypatch, sessions, "_verify")
    with pytest.raises(Crash):
        sync(pair.name)
    monkeypatch.setattr(sessions, "_verify", real)
    turns = pair.codex.turn_ids()
    outcome, p = sync(pair.name)
    assert p.pending is None and outcome.state == "idle", outcome.text
    assert pair.codex.turn_ids() == turns  # duologue's own records did not travel back
    assert prompts(pair.claude.records()).count("from codex") == 1


def test_a_lost_journal_is_settled_by_the_ids_it_wrote(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads.finish

    def finish_then_crash(*args):
        real(*args)
        raise Crash

    monkeypatch.setattr(threads, "finish", finish_then_crash)
    with pytest.raises(Crash):
        sync(pair.name)
    monkeypatch.setattr(threads, "finish", real)
    e = engine()
    e.journal_file(e.pairs[pair.name]).unlink()  # stopped just as it forgot the bytes
    outcome, p = sync(pair.name)
    assert p.pending is None and outcome.state == "idle", outcome.text


def test_a_damaged_journal_blocks_and_release_takes_the_files_as_they_are(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads._write
    crash_in(monkeypatch, threads, "_write")
    with pytest.raises(Crash):
        sync(pair.name)
    monkeypatch.setattr(threads, "_write", real)
    e = engine()
    e.journal_file(e.pairs[pair.name]).write_bytes(b"garbage")
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "missing or damaged" in outcome.text
    e = engine()
    said = linking.release(e, e.pairs[pair.name])
    assert "settled as the files stand" in said
    assert engine().pairs[pair.name].pending is None
    before = len(pair.codex.turn_ids())
    outcome, _ = sync(pair.name)  # the write was never proven: the turn is copied again
    assert outcome.state == "synced" and len(pair.codex.turn_ids()) == before + 1


def test_claude_opening_the_session_during_a_write_is_undone_and_retried(pair, monkeypatch):
    pair.codex.turn("from codex", "ok")
    answers = iter([None, None, 4242])  # free before the write, open right after it
    monkeypatch.setattr("duologue.engine.live_pid", lambda *a: next(answers, None))
    outcome, p = sync(pair.name)
    assert outcome.state == "waiting" and "opened the session" in outcome.text
    pair.claude.turn("typed meanwhile", "ok")  # Claude writes where duologue's records were
    monkeypatch.setattr("duologue.engine.live_pid", lambda *a: None)
    outcome, p = sync(pair.name)
    assert p.pending is None and outcome.state in ("conflict", "synced"), outcome.text


# -------------------------------------------------------------------- rewrites, again
def test_a_codex_rewrite_after_syncs_replays_nothing(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"  # the cursor's anchor is now a synced turn
    text = pair.codex.path.read_text()
    pair.codex.path.write_text(text.replace('"cwd":"/work/project"', '"cwd":"/work/project/."', 1))
    pair.codex.turn("two", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn" in outcome.text
    asked = prompts(pair.claude.records())
    assert asked.count("one") == 1 and asked.count("two") == 1


def test_a_codex_rewrite_that_loses_the_synced_turn_blocks(pair):
    pair.codex.turn("one", "ok")
    sync(pair.name)
    lines = pair.codex.path.read_text().splitlines()
    pair.codex.path.write_text("\n".join(lines[:3]) + "\n")  # the synced turns are gone
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "changed before" in outcome.text


# --------------------------------------------------------------------- linking, again
def test_linking_during_a_running_claude_turn_copies_that_turn_later(homes, claude_running):
    claude = Claude(homes.claude)
    claude.turn("first", "done")
    claude.t.user("in progress")
    claude.save()
    CodexThread(homes.codex, homes.bin)
    e = engine()
    p, _ = linking.link(e, claude=SESSION, codex=THREAD, name="x", cwd=CWD)
    claude.t.assistant("now done")
    claude.save()
    claude_running.stdin.close()
    claude_running.wait(10)
    outcome, _ = sync("x")
    assert outcome.state == "synced"
    rollout = Codex(homes.codex, homes.bin).rollout(THREAD).read_text()
    assert "in progress" in rollout and "now done" in rollout


def test_link_a_large_codex_thread_keeps_the_newest_turns_with_a_note(homes):
    codex = CodexThread(homes.codex, homes.bin)
    for n in range(4):
        codex.turn(f"step {n} " + "y" * 400, "ok")
    with open(homes.duo / "config.toml", "a") as fh:
        fh.write("copy_budget = 1500\n")
    e = engine()
    p, said = linking.link(e, claude=None, codex=THREAD, name="big", cwd=CWD)
    assert "older turn(s) left out" in said
    text = find_transcript(homes.claude, p.claude).read_text()
    assert "earlier turn(s) are not copied here" in text and "step 3" in text
    assert "step 0" not in text


def test_link_last_picks_the_newest_in_the_folder(homes):
    import os
    import time

    old = (
        homes.claude
        / "projects"
        / project_folder(CWD)
        / "00000000-0000-4000-8000-000000000001.jsonl"
    )
    Claude(homes.claude).turn("hi", "hello")
    old.write_text(Claude(homes.claude).path.read_text())
    past = time.time() - 600
    os.utime(old, (past, past))
    e = engine()
    assert linking.find_claude(e, "last", CWD) == SESSION


def test_an_interrupted_link_is_finished_by_the_next_sync(homes, monkeypatch):
    from duologue.codex import threads

    claude = Claude(homes.claude)
    claude.turn("build it", "built")
    real = threads.finish
    crash_in(monkeypatch, threads, "finish")
    with pytest.raises(Crash):
        linking.link(engine(), claude=SESSION, codex=None, name="half", cwd=CWD)
    monkeypatch.setattr(threads, "finish", real)
    p = engine().pairs["half"]
    assert p.pending is not None  # recorded before the thread was written
    outcome, p = sync("half")
    assert p.pending is None and outcome.state == "idle", outcome.text
    assert len(Codex(homes.codex, homes.bin).index(p.codex).turns) == 1
    with pytest.raises(linking.LinkError, match="already linked"):
        linking.link(engine(), claude=SESSION, codex=None, name=None, cwd=CWD)


def test_the_format_alert_goes_out_once(pair, homes, monkeypatch):
    sent = homes.duo / "sent.txt"
    with open(homes.duo / "config.toml", "a") as fh:
        fh.write(f'notify = ["/bin/sh", "-c", "echo \\"$0\\" >> {sent}"]\n')
    monkeypatch.setenv("FAKE_CODEX_SCHEMA", "changed")
    pair.claude.turn("second", "ok")
    for _ in range(3):  # three service passes: three processes, one alert
        assert sync(pair.name)[0].state == "waiting"
    assert sent.read_text().count("has not been tested") == 1


def test_resolve_keep_claude_waits_for_a_running_codex_turn(pair):
    pair.claude.turn("in claude", "ok")
    pair.codex.turn("in codex", "ok")
    sync(pair.name)
    with open(pair.codex.path, "a") as fh:  # a Codex turn starts and has not ended
        start = pair.codex._turn("running", "later")[0]
        fh.write(json.dumps(dict(start, ordinal=999), separators=(",", ":")) + "\n")
    e = engine()
    with pytest.raises(linking.LinkError, match="still running"):
        linking.resolve(e, e.pairs[pair.name], "claude")


def test_link_a_codex_thread_with_nothing_to_copy_creates_nothing(homes):
    x = CodexRollout(THREAD)
    x.start("aborted")
    x._line("event_msg", {"type": "turn_aborted", "turn_id": "aborted", "reason": "interrupted"})
    write_rollout(homes.codex, THREAD, x.records)
    with pytest.raises(linking.LinkError, match="nothing to copy"):
        linking.link(engine(), claude=None, codex=THREAD, name=None, cwd=CWD)
    assert engine().pairs == {}
    assert (
        not list((homes.claude / "projects").rglob("*.jsonl"))
        if (homes.claude / "projects").exists()
        else True
    )


def test_a_legacy_thread_takes_its_first_claude_turn_in_one_pass(homes):
    claude = Claude(homes.claude)
    claude.turn("first", "done")
    codex = CodexThread(homes.codex, homes.bin)
    text = codex.path.read_text().replace('"history_mode":"paginated"', '"history_mode":"legacy"')
    codex.path.write_text(text)
    e = engine()
    linking.link(e, claude=SESSION, codex=THREAD, name="old", cwd=CWD)
    claude.turn("second", "ok")
    outcome, _ = sync("old")
    assert outcome.state == "synced", outcome.text


def test_a_source_rewritten_while_linking_blocks_the_new_pair(homes, monkeypatch):
    from duologue.codex import threads

    claude = Claude(homes.claude)
    claude.turn("build it", "built")
    real = threads.finish

    def rewrite_then_finish(*args):
        real(*args)
        data = claude.path.read_bytes()
        claude.path.write_bytes(data.replace(b"build it", b"BUILD IT"))

    monkeypatch.setattr(threads, "finish", rewrite_then_finish)
    p, _ = linking.link(engine(), claude=SESSION, codex=None, name="moved", cwd=CWD)
    assert (
        engine().pairs["moved"].blocked
        and "changed while duologue copied" in engine().pairs["moved"].blocked
    )


def test_a_codex_turn_that_finishes_after_the_pass_read_is_not_skipped(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("in claude", "ok")
    real = threads._caught_up

    def codex_turn_lands_first(codex, thread_id, path):  # a quick `codex exec` meanwhile
        pair.codex.turn("in codex meanwhile", "ok")
        return real(codex, thread_id, path)

    monkeypatch.setattr(threads, "_caught_up", codex_turn_lands_first)
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "changed" in outcome.text
    monkeypatch.setattr(threads, "_caught_up", real)
    outcome, _ = sync(pair.name)
    assert outcome.state == "conflict"  # both sides wrote: seen, not skipped


def test_release_will_not_skip_lines_codex_indexed_only_in_part(pair, monkeypatch):
    pair.claude.turn("second", "ok")
    monkeypatch.setenv("FAKE_CODEX_LEAVE_LINES", "1")  # Codex leaves our last line unindexed
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked"
    size = pair.codex.path.stat().st_size
    e = engine()
    with pytest.raises(Blocked, match="has not indexed"):
        linking.release(e, e.pairs[pair.name])
    assert engine().pairs[pair.name].blocked  # still blocked, file untouched
    assert pair.codex.path.stat().st_size == size


def test_release_after_codex_took_nothing_copies_the_turn_again(pair, monkeypatch):
    pair.claude.turn("second", "ok")
    before = len(pair.codex.turn_ids())
    monkeypatch.setenv("FAKE_CODEX_NO_INDEX", "1")
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "taken out" in outcome.text
    monkeypatch.delenv("FAKE_CODEX_NO_INDEX")
    e = engine()
    linking.release(e, e.pairs[pair.name])
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and len(pair.codex.turn_ids()) == before + 1


# ------------------------------------------------------------------ what Codex does to threads
def compress(path: Path) -> Path:
    """Codex compresses an idle thread's rollout in place (gzip here: the fake reads it back)."""
    packed = path.with_name(path.name + ".zst")
    packed.write_bytes(gzip.compress(path.read_bytes()))
    path.unlink()
    return packed


def requests(log: Path) -> list[str]:
    return [json.loads(x)["method"] for x in log.read_text().splitlines()] if log.exists() else []


def test_a_compressed_thread_takes_claudes_turn_once_codex_writes_it_out(pair):
    turns = pair.codex.turn_ids()
    packed = compress(pair.codex.path)
    pair.claude.turn("second", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert pair.codex.path.exists() and not packed.exists()
    assert len(pair.codex.turn_ids()) == len(turns) + 1
    assert "wrote the compressed thread out" in json.dumps(
        ActivityLog(engine().config.log_file).tail(5)
    )


def test_a_compressed_thread_with_nothing_to_send_is_left_compressed(pair, tmp_path, monkeypatch):
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    packed = compress(pair.codex.path)
    outcome, _ = sync(pair.name)
    assert outcome.state == "idle", outcome.text
    assert packed.exists() and "thread/resume" not in requests(log)


def test_a_thread_rewound_in_codex_blocks_and_says_so(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    # Codex continues a rewound thread in a new file that names the history it keeps.
    rewound = pair.codex.path.with_name(
        pair.codex.path.name.replace(".jsonl", "_01a1ab4d-0000-7000-8000-000000000001.jsonl")
    )
    base = {"thread_id": THREAD, "end_ordinal_exclusive": 3}
    meta = {"id": THREAD, "cwd": CWD, "history_mode": "paginated", "history_base": base}
    first = {"timestamp": "2026-11-04T16:00:00.000Z", "type": "session_meta", "payload": meta}
    rewound.write_text(json.dumps(dict(first, ordinal=3)) + "\n")
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "rewound in Codex" in outcome.text


def test_a_codex_rollback_of_synced_turns_is_told_to_claude(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    pair.codex.turn("one, edited", "ok", before=(pair.codex.rollback(1),))
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    texts = json.dumps(pair.claude.records()[-6:], ensure_ascii=False)
    assert 'before this, the turn(s) that began \\"one\\" were undone in Codex' in texts
    assert prompts(pair.claude.records())[-1] == "one, edited"


def test_a_codex_turn_rolled_back_before_it_synced_never_reaches_claude(pair):
    pair.codex.turn("a mistake", "ok")
    pair.codex.turn("what I meant", "ok", before=(pair.codex.rollback(1),))
    outcome, p = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn(s)" in outcome.text, outcome.text
    assert p.copied["to_claude"] == 1  # the turns delivered, not the turns in the batch
    asked = prompts(pair.claude.records())
    assert "a mistake" not in asked and asked[-1] == "what I meant"
    assert "undone" not in json.dumps(pair.claude.records()[-6:])


def test_a_claude_turn_after_a_codex_rollback_is_a_conflict(pair):
    pair.claude.turn("in claude", "ok")
    started = {"type": "task_started", "turn_id": "codex-empty", "started_at": 1}
    ended = {"type": "turn_aborted", "turn_id": "codex-empty", "reason": "interrupted"}
    pair.codex.add(
        [
            pair.codex.rollback(1),
            {"timestamp": "2026-11-04T15:00:01.000Z", "type": "event_msg", "payload": started},
            {"timestamp": "2026-11-04T15:00:02.000Z", "type": "event_msg", "payload": ended},
        ]
    )
    outcome, p = sync(pair.name)
    assert outcome.state == "conflict" and p.conflict


# --------------------------------------------------------------------------- echo guard
def rewind_cursor(name: str, side: str, end: int) -> None:
    from duologue.journal import prefix_hash
    from duologue.pairs import Cursor

    e = engine()
    p = e.pairs[name]
    path = e.paths(p)[0 if side == "claude" else 1]
    setattr(p, f"{side}_cursor", Cursor(end, prefix_hash(path, end), None))
    p.seen, p.checked = [], {}
    e.save()


def test_records_duologue_wrote_never_go_back_to_codex(pair):
    size = pair.claude.path.stat().st_size
    pair.codex.turn("from codex", "ok")
    assert sync(pair.name)[0].state == "synced"
    turns = pair.codex.turn_ids()
    rewind_cursor(pair.name, "claude", size)  # as if the cursor sat before duologue's records
    outcome, _ = sync(pair.name)
    assert outcome.state == "idle", outcome.text
    assert pair.codex.turn_ids() == turns


def test_turns_duologue_wrote_never_go_back_to_claude(pair):
    size = pair.codex.path.stat().st_size
    pair.claude.turn("from claude", "ok")
    assert sync(pair.name)[0].state == "synced"
    records = len(pair.claude.records())
    rewind_cursor(pair.name, "codex", size)
    outcome, _ = sync(pair.name)
    assert outcome.state == "idle", outcome.text
    assert len(pair.claude.records()) == records


def test_a_codex_line_numbered_out_of_order_blocks_the_append(pair):
    with open(pair.codex.path, "a") as fh:  # a line whose number goes back: the index skips it
        fh.write(json.dumps(dict(pair.codex.rollback(0), ordinal=0)) + "\n")
    resume(pair.codex.codex, THREAD)
    pair.claude.turn("second", "ok")
    size = pair.codex.path.stat().st_size
    outcome, _ = sync(pair.name)
    assert outcome.state == "blocked" and "Codex's index expects" in outcome.text
    assert pair.codex.path.stat().st_size == size


# ------------------------------------------------------------- after the pre-merge review
def test_an_undo_in_codex_with_nothing_after_it_conflicts_with_a_claude_turn(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    pair.codex.add([pair.codex.rollback(1)])  # undone in Codex, nothing sent yet
    pair.claude.turn("in claude", "ok")
    outcome, p = sync(pair.name)
    assert outcome.state == "conflict" and p.conflict


def test_an_undo_in_codex_with_nothing_after_it_is_told_to_claude_once(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    pair.codex.add([pair.codex.rollback(1)])
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Codex undid 1 turn(s)" in outcome.text
    said = json.dumps(pair.claude.records()[-3:], ensure_ascii=False)
    assert 'in Codex, the turn(s) that began \\"one\\" were undone' in said
    pair.codex.turn("two", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn(s)" in outcome.text
    assert json.dumps(pair.claude.records(), ensure_ascii=False).count("were undone") == 1


def test_codex_turns_waiting_in_a_compressed_thread_still_reach_claude(pair):
    pair.codex.turn("from codex", "ok")  # not synced before Codex compresses the thread
    compress(pair.codex.path)
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert "from codex" in prompts(pair.claude.records())


def test_an_archived_compressed_thread_is_left_alone(pair, homes, tmp_path, monkeypatch):
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    archived = homes.codex / "archived_sessions" / pair.codex.path.name
    archived.parent.mkdir()
    pair.codex.path.rename(archived)
    compress(archived)
    pair.claude.turn("second", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "archived" in outcome.text
    assert "thread/resume" not in requests(log)


def test_resolve_and_release_write_a_compressed_thread_out(pair):
    pair.claude.turn("in claude", "ok")
    pair.codex.turn("in codex", "ok")
    assert sync(pair.name)[0].state == "conflict"
    compress(pair.codex.path)
    e = engine()
    linking.resolve(e, e.pairs[pair.name], "codex")
    assert pair.codex.path.exists()
    compress(pair.codex.path)
    e = engine()
    e.pairs[pair.name].blocked = "for the test"
    e.save()
    assert "unblocked" in linking.release(e, e.pairs[pair.name])


def test_a_waiting_pass_does_not_translate_codex_turns(pair, claude_running, monkeypatch):
    from duologue import engine as engine_module

    def refuse(*args, **kwargs):
        raise AssertionError("translated while Claude has the session open")

    monkeypatch.setattr(engine_module, "codex_to_claude_steps", refuse)
    pair.codex.turn("from codex", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting", outcome.text


def test_link_from_codex_leaves_out_turns_rolled_back_across_several(homes):
    codex = CodexThread(homes.codex, homes.bin)
    codex.turn("keep me", "ok")
    codex.turn("withdrawn A", "ok")
    codex.turn("withdrawn B", "ok")
    codex.turn("edited", "ok", before=(codex.rollback(2),))
    e = engine()
    p, said = linking.link(e, claude=None, codex=THREAD, name=None, cwd=CWD)
    asked = prompts([ln.record for ln in read_lines(find_transcript(homes.claude, p.claude))])
    assert asked == ["hello", "keep me", "edited"]
    assert "with 3 turn(s)" in said


def test_link_from_a_rewound_codex_thread_is_refused(homes):
    codex = CodexThread(homes.codex, homes.bin)
    rewound = codex.path.with_name(
        codex.path.name.replace(".jsonl", "_01a1ab4d-0000-7000-8000-000000000001.jsonl")
    )
    base = {"thread_id": THREAD, "end_ordinal_exclusive": 3}
    meta = {"id": THREAD, "cwd": CWD, "history_mode": "paginated", "history_base": base}
    first = {"timestamp": "2026-11-04T16:00:00.000Z", "type": "session_meta", "payload": meta}
    rewound.write_text(json.dumps(dict(first, ordinal=3)) + "\n")
    with pytest.raises(linking.LinkError, match="rewound"):
        linking.link(engine(), claude=None, codex=THREAD, name=None, cwd=CWD)


def test_link_from_claude_keeps_a_queued_message_in_its_turn(homes):
    claude = Claude(homes.claude)
    claude.t.user("build it")
    claude.t.call("Bash", {"command": "make"}, "built")
    claude.t.queued("and test it too")
    claude.t.assistant("built and tested")
    claude.save()
    p, said = linking.link(engine(), claude=SESSION, codex=None, name="q", cwd=CWD)
    assert "with 1 turn(s)" in said
    path = Codex(homes.codex, homes.bin).rollout(p.codex)
    said_in_codex = [i["content"][0]["text"] for i in items(path) if i["type"] == "UserMessage"]
    assert said_in_codex == ["build it", "and test it too"]


def test_a_compressed_thread_is_not_resumed_while_claude_is_still_working(
    pair, claude_running, tmp_path, monkeypatch
):
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    packed = compress(pair.codex.path)
    pair.claude.t.user("still being answered")
    pair.claude.save()
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "working" in outcome.text
    assert packed.exists() and "thread/resume" not in requests(log)


def test_a_compressed_thread_codex_did_not_write_out_waits(pair, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_NO_EXPAND", "1")
    compress(pair.codex.path)
    pair.claude.turn("second", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "did not write it out" in outcome.text


def test_records_duologue_wrote_stay_out_of_a_claude_turn_sent_after_them(pair):
    size = pair.claude.path.stat().st_size
    pair.codex.turn("from codex", "ok")
    assert sync(pair.name)[0].state == "synced"
    turns = pair.codex.turn_ids()
    rewind_cursor(pair.name, "claude", size)
    pair.claude.turn("typed in claude", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert len(pair.codex.turn_ids()) == len(turns) + 1
    said = [i["content"][0]["text"] for i in items(pair.codex.path) if i["type"] == "UserMessage"]
    assert said.count("from codex") == 1 and said[-1] == "typed in claude"


# ------------------------------------------------------- after the second review round
def an_undo_conflict(pair) -> None:
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    pair.codex.add([pair.codex.rollback(1)])
    pair.claude.turn("in claude", "ok")
    assert sync(pair.name)[0].state == "conflict"


def test_keeping_claude_after_an_undo_conflict_sends_claudes_turn(pair):
    an_undo_conflict(pair)
    turns = pair.codex.turn_ids()
    e = engine()
    linking.resolve(e, e.pairs[pair.name], "claude")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Claude turn" in outcome.text, outcome.text
    assert len(pair.codex.turn_ids()) == len(turns) + 1


def test_keeping_codex_after_an_undo_conflict_tells_claude_then_moves_on(pair):
    an_undo_conflict(pair)
    e = engine()
    linking.resolve(e, e.pairs[pair.name], "codex")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Codex undid 1 turn(s)" in outcome.text
    pair.claude.turn("after the undo", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "Claude turn" in outcome.text, outcome.text


def test_a_codex_thread_linked_after_an_undo_starts_after_it(homes):
    codex = CodexThread(homes.codex, homes.bin)
    codex.turn("withdrawn", "ok")
    codex.add([codex.rollback(1)])
    p, _ = linking.link(engine(), claude=None, codex=THREAD, name="u", cwd=CWD)
    assert engine().pairs["u"].codex_cursor.end == codex.path.stat().st_size
    claude = Claude(homes.claude)
    claude.path = find_transcript(homes.claude, p.claude)
    claude.t = ClaudeTranscript(session=p.claude)
    claude.turn("in claude", "ok")
    outcome, _ = sync("u")
    assert outcome.state == "synced" and "Claude turn" in outcome.text, outcome.text


def test_an_undo_already_told_is_not_told_again_after_codex_rewrites_the_thread(pair):
    pair.codex.turn("two", "ok")
    pair.codex.turn("three", "ok", before=())
    pair.codex.add([pair.codex.rollback(1)])  # "three" undone before it ever synced
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    text = pair.codex.path.read_text()  # Codex rewrites the thread, as when it converts it
    pair.codex.path.write_text(text.replace('"cwd":"/work/project"', '"cwd":"/work/project/."', 1))
    pair.codex.turn("four", "ok")
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn(s)" in outcome.text, outcome.text
    assert "were undone" not in json.dumps(pair.claude.records(), ensure_ascii=False)


def test_a_compressed_thread_is_not_resumed_over_an_unsettled_write(pair, tmp_path, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads._write
    crash_in(monkeypatch, threads, "_write")
    with pytest.raises(Crash):
        sync(pair.name)
    monkeypatch.setattr(threads, "_write", real)
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    packed = compress(pair.codex.path)
    for _ in range(2):  # settling waits; so does the pass's own read
        outcome, p = sync(pair.name)
        assert outcome.state == "waiting" and p.pending is not None, outcome.text
    assert packed.exists() and "thread/resume" not in requests(log)


def test_a_compressed_thread_with_nothing_to_copy_is_resumed_once_at_most(
    pair, tmp_path, monkeypatch
):
    started = {"type": "task_started", "turn_id": "codex-left-open", "started_at": 1}
    pair.codex.add(  # a turn a stopped Codex left open: it never travels
        [{"timestamp": "2026-11-04T15:00:01.000Z", "type": "event_msg", "payload": started}]
    )
    assert sync(pair.name)[0].state == "idle"
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    compress(pair.codex.path)
    assert sync(pair.name)[0].state == "idle"
    assert "thread/resume" not in requests(log)


def test_codex_turns_in_a_compressed_thread_reach_claude_whatever_the_codex_format(
    pair, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_SCHEMA", "changed")  # only writes to Codex wait for that
    pair.codex.turn("from codex", "ok")
    compress(pair.codex.path)
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced", outcome.text
    assert "from codex" in prompts(pair.claude.records())


# -------------------------------------------------------- after the third review round
def test_an_undo_told_after_a_kept_codex_side_names_the_turn_codex_undid(pair):
    an_undo_conflict(pair)  # "one" synced, undone in Codex, then "in claude" typed in Claude
    e = engine()
    linking.resolve(e, e.pairs[pair.name], "codex")
    assert sync(pair.name)[0].state == "synced"
    last = json.dumps(pair.claude.records()[-3:], ensure_ascii=False)
    assert 'began \\"one\\"' in last and "in claude" not in last


def test_an_edit_and_resend_arrives_in_one_piece(pair):
    pair.codex.turn("one", "ok")
    assert sync(pair.name)[0].state == "synced"
    started = {"type": "task_started", "turn_id": "codex-edited", "started_at": 1}
    pair.codex.add(  # the undo, then the edited turn still running
        [
            pair.codex.rollback(1),
            {"timestamp": "2026-11-04T15:00:01.000Z", "type": "event_msg", "payload": started},
        ]
    )
    outcome, _ = sync(pair.name)
    assert outcome.state == "waiting" and "running after an undo" in outcome.text
    x = CodexRollout(THREAD, start_ms=1_793_900_900_000)
    x.start("codex-edited")
    x.user_says("one, edited", "edited-u")
    x.agent_says("ok", "edited-a")
    x.complete("ok")
    pair.codex.add(x.records[1:])  # the edited turn ends (its task_started is already there)
    outcome, _ = sync(pair.name)
    assert outcome.state == "synced" and "copied 1 Codex turn(s)" in outcome.text, outcome.text
    texts = json.dumps(pair.claude.records(), ensure_ascii=False)
    assert texts.count("were undone in Codex") == 1 and "nothing else" not in texts
    assert prompts(pair.claude.records())[-1] == "one, edited"


def test_a_write_codex_indexed_whole_settles_although_the_thread_is_compressed(pair, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")
    real = threads.finish
    crash_in(monkeypatch, threads, "finish")  # written, then the pass stops before confirming
    with pytest.raises(Crash):
        sync(pair.name)
    monkeypatch.setattr(threads, "finish", real)  # not undo(): that drops the test's homes too
    resume(pair.codex.codex, THREAD)  # Codex indexes it (as when the thread is opened)
    compress(pair.codex.path)
    outcome, p = sync(pair.name)
    assert p.pending is None, outcome.text
    assert outcome.state in ("idle", "synced"), outcome.text


def test_release_will_not_have_codex_index_an_unsettled_write(pair, tmp_path, monkeypatch):
    from duologue.codex import threads

    pair.claude.turn("second", "ok")

    def torn(path, intent):  # half the lines land, then duologue stops
        with open(path, "ab") as fh:
            fh.write(intent.data[: len(intent.data) // 2])
        raise Crash

    monkeypatch.setattr(threads, "_write", torn)
    with pytest.raises(Crash):
        sync(pair.name)
    e = engine()
    e.journal_file(e.pairs[pair.name]).unlink()  # and its record of the write is lost
    assert sync(pair.name)[0].state == "blocked"
    log = tmp_path / "requests.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    packed = compress(pair.codex.path)
    e = engine()
    with pytest.raises(Wait, match="not settled"):
        linking.release(e, e.pairs[pair.name])
    assert packed.exists() and "thread/resume" not in requests(log)
