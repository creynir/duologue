"""Pairing conversations by themselves, and keeping both sides' turns after the user wrote in both
apps: the defaults, against the fake codex in throwaway homes."""

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from builders import SESSION, ClaudeTranscript, CodexRollout
from conftest import write_rollout
from test_engine import (  # noqa: F401 (fixture)
    THREAD,
    Claude,
    CodexThread,
    engine,
    homes,
    prompts,
    sync,
)

from duologue import auto, linking
from duologue.claude.transcript import find_transcript, project_folder
from duologue.cli import main
from duologue.codex.threads import Codex, resume
from duologue.engine import ALONGSIDE_CLAUDE, ALONGSIDE_CODEX
from duologue.jsonl import encode, read_lines

pytestmark = pytest.mark.integration

OTHER = "5e550000-0000-4000-8000-000000000002"
THREAD_2 = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f9700"


@pytest.fixture
def auto_homes(homes, tmp_path: Path):  # noqa: F811
    """The homes with duologue's defaults: automatic pairing, both sides kept."""
    cfg = homes.duo / "config.toml"
    cfg.write_text(
        cfg.read_text().replace("auto = false\n", "").replace('on_conflict = "stop"\n', "")
    )
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(**vars(homes), work=str(work))


def claude_session(h, session: str = SESSION, cwd: str | None = None, **extra) -> Path:
    """A Claude session with one finished turn, in ``cwd`` (default: the work folder)."""
    cwd = cwd or h.work
    t = ClaudeTranscript(cwd=cwd, session=session)
    t.user("build the thing", **extra)
    t.assistant("built", **extra)
    path = h.claude / "projects" / project_folder(cwd) / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(t.records))
    return path


def codex_thread(
    h, thread: str = THREAD, cwd: str | None = None, meta: dict | None = None, turn: str = "t-1"
) -> Path:
    """A Codex thread with one finished turn, in ``cwd`` (default: the work folder), indexed."""
    x = CodexRollout(thread)
    x.start(turn)
    x.user_says("hello codex", f"{turn}-u")
    x.agent_says("hi", f"{turn}-a")
    x.complete("hi")
    path = write_rollout(h.codex, thread, x.records, cwd=cwd or h.work, meta=meta)
    resume(Codex(h.codex, h.bin), thread)
    return path


def run(*argv: str) -> tuple[int, str]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


def pairs() -> dict:
    return engine().pairs


# ------------------------------------------------------------------------------- pairing
def test_sync_pairs_new_conversations_on_both_sides_once(auto_homes):
    claude_session(auto_homes)
    codex_thread(auto_homes)
    code, out = run("sync")
    assert code == 0, out
    assert "created Codex thread" in out and "created Claude session" in out
    found = pairs()
    assert {p.claude for p in found.values()} >= {SESSION}
    assert {p.codex for p in found.values()} >= {THREAD}
    assert len(found) == 2
    # What duologue created is never paired again, and nothing new means nothing to say.
    code, out = run("sync", "--quiet")
    assert code == 0 and out == "", out
    assert len(pairs()) == 2


def test_what_is_left_alone(auto_homes, tmp_path):
    old = claude_session(auto_homes, session=OTHER)
    eight_days = time.time() - 8 * 86_400
    os.utime(old, (eight_days, eight_days))
    claude_session(auto_homes, entrypoint="sdk-cli")  # claude -p
    codex_thread(auto_homes, meta={"source": "exec", "originator": "codex_exec"})
    codex_thread(auto_homes, thread=THREAD_2, meta={"source": {"subagent": {"thread_spawn": {}}}})
    code, out = run("sync")
    assert code == 0 and pairs() == {}, out
    # Scripted runs are paired once the user asks for them; old and subagent ones still are not.
    with open(auto_homes.duo / "config.toml", "a") as fh:
        fh.write("auto_scripted = true\n")
    code, out = run("sync")
    assert code == 0, out
    got = pairs().values()
    assert {p.claude for p in got} >= {SESSION} and {p.codex for p in got} >= {THREAD}
    assert OTHER not in {p.claude for p in got} and THREAD_2 not in {p.codex for p in got}


def test_a_folder_that_is_gone_or_a_conversation_duologue_made_is_left_alone(auto_homes):
    claude_session(auto_homes, cwd="/no/such/folder")
    codex_thread(auto_homes, meta={"originator": "duologue"})
    code, out = run("sync")
    assert code == 0 and pairs() == {}, out


def test_an_unlinked_pair_is_not_paired_again_until_linked(auto_homes):
    claude_session(auto_homes)
    assert run("sync")[0] == 0
    [pair] = pairs().values()
    code, out = run("unlink", pair.name)
    assert code == 0 and "does not pair them again" in out
    assert run("sync")[0] == 0
    assert pairs() == {}
    code, out = run("link", "--claude", SESSION)
    assert code == 0, out
    assert [p.claude for p in pairs().values()] == [SESSION]


def test_a_conversation_passed_over_is_looked_at_again_once_it_changes(auto_homes):
    path = claude_session(auto_homes)
    records = [ln.record for ln in read_lines(path)]
    path.write_bytes(encode([dict(records[0], isMeta=True)]))  # nothing typed yet
    assert run("sync")[0] == 0 and pairs() == {}
    passed = auto.AutoState.load(auto_homes.duo / "auto.json").passed
    assert passed[str(path)][2] == "nothing was typed in it yet"
    path.write_bytes(encode(records))
    assert run("sync")[0] == 0
    assert [p.claude for p in pairs().values()] == [SESSION]


def test_one_pass_pairs_at_most_ten(auto_homes, monkeypatch):
    monkeypatch.setattr(auto, "PER_PASS", 2)
    for n in range(3):
        claude_session(auto_homes, session=f"5e550000-0000-4000-8000-00000000010{n}")
    run("sync")
    assert len(pairs()) == 2
    run("sync")
    assert len(pairs()) == 3


def test_a_conversation_that_cannot_be_paired_is_logged_once(auto_homes, monkeypatch):
    claude_session(auto_homes)

    def refuse(*args, **kwargs):
        raise linking.LinkError("refused for the test")

    monkeypatch.setattr(linking, "link", refuse)
    run("sync")
    run("sync", "--force")
    entries = [json.loads(x) for x in (auto_homes.duo / "log.jsonl").read_text().splitlines()]
    assert sum("refused for the test" in x["text"] for x in entries) == 1


# --------------------------------------------------------------------- Codex's own importer
def imported(h, session_path: Path, thread: str = THREAD) -> Path:
    """A thread as Codex's importer makes one of a Claude session, and its ledger record."""
    x = CodexRollout(thread)
    x.start("external-import-turn-1")
    said = [{"type": "input_text", "text": "build the thing"}]
    x.response({"type": "message", "role": "user", "content": said})
    x.response(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "built"}],
        }
    )
    x.complete("built")
    path = write_rollout(h.codex, thread, x.records, cwd=h.work, history_mode="legacy")
    resume(Codex(h.codex, h.bin), thread)
    ledger = {
        "records": [
            {
                "source_path": str(session_path),
                "content_sha256": hashlib.sha256(session_path.read_bytes()).hexdigest(),
                "imported_thread_id": thread,
                "imported_at": 1,
            }
        ]
    }
    (h.codex / "external_agent_session_imports.json").write_text(json.dumps(ledger))
    return path


def add_claude_turn(h, spath: Path, prompt: str) -> None:
    t = ClaudeTranscript(cwd=h.work)
    t._n = 10 + len(read_lines(spath))
    t._parent = [ln.record for ln in read_lines(spath)][-1]["uuid"]
    t.user(prompt)
    t.assistant("done")
    with open(spath, "ab") as fh:
        fh.write(encode(t.records))


def test_a_session_codex_imported_is_paired_with_that_thread(auto_homes):
    spath = claude_session(auto_homes)
    imported(auto_homes, spath)
    add_claude_turn(auto_homes, spath, "and test it")  # after Codex copied it
    code, out = run("sync")  # pairs them, then copies the turn Codex did not
    assert code == 0, out
    assert "paired with the thread Codex's importer made of it" in out
    assert "copied 1 Claude turn(s) to Codex" in out, out
    [pair] = pairs().values()
    assert (pair.claude, pair.codex) == (SESSION, THREAD)
    before = len(read_lines(spath))
    # A new Claude turn goes to that thread once; the imported turn never goes back to Claude.
    add_claude_turn(auto_homes, spath, "and ship it")
    code, out = run("sync")
    assert code == 0 and "copied 1 Claude turn(s) to Codex" in out, out
    turns = list(Codex(auto_homes.codex, auto_homes.bin).index(THREAD).turns)
    assert len(turns) == 3 and turns[0] == "external-import-turn-1"
    assert len(read_lines(spath)) == before + 2
    assert run("sync", "--quiet") == (0, "")


def test_turns_made_in_codex_after_its_import_reach_claude(auto_homes):
    spath = claude_session(auto_homes)
    xpath = imported(auto_homes, spath)
    x = CodexRollout(THREAD, start_ms=1_793_900_000_000)
    x.start("codex-own-turn")
    x.user_says("carried on in codex", "own-u")
    x.agent_says("noted", "own-a")
    x.complete("noted")
    with open(xpath, "a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in x.records))
    resume(Codex(auto_homes.codex, auto_homes.bin), THREAD)
    code, out = run("sync")  # pairs them, then syncs the new pair
    assert code == 0 and "paired with the thread" in out, out
    assert "copied 1 Codex turn(s) to Claude" in out, out
    said = prompts([ln.record for ln in read_lines(spath)])
    assert said == ["build the thing", "carried on in codex"]


def test_a_session_changed_since_codex_imported_it_gets_a_thread_of_its_own(auto_homes):
    spath = claude_session(auto_homes)
    imported(auto_homes, spath)
    records = [ln.record for ln in read_lines(spath)]
    spath.write_bytes(encode([dict(records[0], timestamp="2026-11-04T14:00:09.000Z"), records[1]]))
    code, out = run("sync")
    assert code == 0 and "created Codex thread" in out and "importer" not in out, out
    [pair] = pairs().values()
    assert pair.claude == SESSION and pair.codex != THREAD
    assert run("sync", "--quiet") == (0, "")  # Codex's own copy is left as it is


def test_claude_turns_since_the_import_are_copied_within_the_budget(auto_homes):
    spath = claude_session(auto_homes)
    imported(auto_homes, spath)
    for n in range(4):
        add_claude_turn(auto_homes, spath, f"step {n} " + "x" * 300)
    with open(auto_homes.duo / "config.toml", "a") as fh:
        fh.write("copy_budget = 1000\n")
    code, out = run("sync")
    assert code == 0, out
    assert "3 Claude turn(s) since the import left out" in out, out
    assert "copied 1 Claude turn(s) to Codex" in out, out
    rollout = Codex(auto_homes.codex, auto_homes.bin).rollout(THREAD).read_text()
    assert "its next 3 turn(s) are not copied here" in rollout and "step 3" in rollout
    assert "step 0" not in rollout
    assert next(iter(pairs().values())).note_to_codex is None
    add_claude_turn(auto_homes, spath, "one more")
    code, out = run("sync")
    assert "copied 1 Claude turn(s) to Codex" in out, out
    again = Codex(auto_homes.codex, auto_homes.bin).rollout(THREAD).read_text()
    assert again.count("are not copied here") == rollout.count("are not copied here")


def test_a_thread_codex_imported_from_a_paired_session_is_left_alone(auto_homes):
    spath = claude_session(auto_homes)
    assert run("sync")[0] == 0  # paired with a thread duologue made
    imported(auto_homes, spath, thread=THREAD_2)
    code, out = run("sync")
    assert code == 0 and "created" not in out, out
    assert len(pairs()) == 1
    claude_files = list((auto_homes.claude / "projects").rglob("*.jsonl"))
    assert claude_files == [spath]


# ------------------------------------------------------------------- writing in both apps
@pytest.fixture
def both(auto_homes):
    claude = Claude(auto_homes.claude)
    claude.turn("first", "done in Claude")
    codex = CodexThread(auto_homes.codex, auto_homes.bin)
    p, _ = linking.link(engine(), claude=SESSION, codex=THREAD, name="both", cwd=auto_homes.work)
    return SimpleNamespace(claude=claude, codex=codex, name=p.name)


def test_turns_written_in_both_apps_are_all_kept(both):
    both.claude.turn("in claude", "ok")
    both.codex.turn("in codex", "ok")
    outcome, p = sync(both.name)
    assert outcome.state == "synced" and "meanwhile" in outcome.text, outcome.text
    assert not p.conflict
    rollout = both.codex.path.read_text()
    assert ALONGSIDE_CODEX in rollout
    assert rollout.index('"in codex"') < rollout.index("in claude")
    outcome, p = sync(both.name)
    assert outcome.state == "synced" and "copied 1 Codex turn(s)" in outcome.text, outcome.text
    records = both.claude.records()
    assert prompts(records)[-2:] == ["in claude", "in codex"]
    assert ALONGSIDE_CLAUDE in json.dumps(records[-8:], ensure_ascii=False)
    turns = both.codex.turn_ids()
    assert sync(both.name)[0].state == "idle"
    assert both.codex.turn_ids() == turns and len(both.claude.records()) == len(records)
    assert p.copied == {"to_codex": 1, "to_claude": 1}


def test_a_claude_turn_after_an_undo_in_codex_keeps_both(both):
    both.codex.turn("one", "ok")
    assert sync(both.name)[0].state == "synced"
    both.claude.turn("in claude", "ok")
    both.codex.add([both.codex.rollback(1)])
    assert sync(both.name)[0].state == "synced"
    outcome, _ = sync(both.name)
    assert outcome.state == "synced" and "undid 1 turn" in outcome.text, outcome.text
    assert "undone" in json.dumps(both.claude.records()[-4:])
    assert sync(both.name)[0].state == "idle"


def test_a_conflict_left_from_stop_is_carried_on_from(both):
    e = engine()
    e.pairs[both.name].conflict = True
    e.save()
    both.claude.turn("in claude", "ok")
    outcome, p = sync(both.name)
    assert outcome.state == "synced" and not p.conflict


def test_the_session_duologue_creates_is_found_by_its_id(auto_homes):
    codex_thread(auto_homes)
    run("sync")
    [pair] = pairs().values()
    path = find_transcript(auto_homes.claude, pair.claude)
    assert path.parent.name == project_folder(auto_homes.work)
