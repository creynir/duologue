"""Syncing pairs end to end against the fake codex and Claude transcripts in throwaway homes."""

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
    (duo / "config.toml").write_text(
        f'codex_bin = "{fake_codex_bin}"\nclaude_bin = "{tmp_path / "no-claude"}"\n'
        "quiet_seconds = 0\n"
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
        if new and there:
            new[0] = dict(new[0], parentUuid=there[-1].get("uuid"))
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

    def turn(self, prompt: str, answer: str) -> None:
        ordinal = self.codex.index(THREAD).next_ordinal
        with open(self.path, "a") as fh:
            for k, r in enumerate(self._turn(prompt, answer)):
                fh.write(json.dumps(dict(r, ordinal=ordinal + k), separators=(",", ":")) + "\n")
        resume(self.codex, THREAD)

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
    added = pair.claude.records()[-5:]
    texts = json.dumps(added, ensure_ascii=False)
    assert MARK in texts and "from codex" in texts and "codex answered" in texts
    assert any(
        b.get("type") == "tool_use" and b.get("name") == "Bash"
        for r in added
        for b in (r["message"]["content"] if isinstance(r["message"]["content"], list) else [])
    )
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
