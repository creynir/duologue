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

from duologue import auto, gate, linking
from duologue.claude.transcript import find_transcript, project_folder
from duologue.cli import main
from duologue.codex import threads
from duologue.codex.rpc import AppServerError
from duologue.codex.threads import Codex, resume
from duologue.engine import ALONGSIDE_CLAUDE, ALONGSIDE_CODEX
from duologue.jsonl import encode, read_lines
from duologue.pairs import AutoState, auto_state_file
from duologue.translate.common import ORIGIN

pytestmark = pytest.mark.integration

OTHER = "5e550000-0000-4000-8000-000000000002"
THREAD_2 = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f9700"


@pytest.fixture
def auto_homes(homes, tmp_path: Path):  # noqa: F811
    """The homes with duologue's defaults: automatic pairing, both sides kept."""
    cfg = homes.duo / "config.toml"
    cfg.write_text(cfg.read_text().replace("auto = false\n", ""))
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


def state() -> AutoState:
    return AutoState.load(auto_state_file(engine().config))


def reasons() -> list[str]:
    return sorted(entry[2] for entry in state().passed.values())


def set_config(h, text: str) -> None:
    with open(h.duo / "config.toml", "a") as fh:
        fh.write(text)


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


def test_the_session_duologue_creates_is_in_the_threads_folder(auto_homes):
    codex_thread(auto_homes)
    run("sync")
    [pair] = pairs().values()
    path = find_transcript(auto_homes.claude, pair.claude)
    assert path.parent.name == project_folder(auto_homes.work)


def test_claude_sessions_left_alone_and_why(auto_homes):
    scripted = claude_session(auto_homes, session=OTHER, entrypoint="sdk-cli")  # claude -p
    gone = claude_session(
        auto_homes, session="5e550000-0000-4000-8000-000000000003", cwd="/no/such/folder"
    )
    ours = claude_session(auto_homes, session="5e550000-0000-4000-8000-000000000004")
    records = [ln.record for ln in read_lines(ours)]
    ours.write_bytes(encode([dict(r, **{ORIGIN: "codex"}) for r in records]))
    moved = claude_session(auto_homes, session="5e550000-0000-4000-8000-000000000005")
    with open(moved, "ab") as fh:
        fh.write(encode([{"type": "continued-in", "sessionId": "x", "continuedInSessionId": "y"}]))
    told = claude_session(auto_homes, session="5e550000-0000-4000-8000-000000000006")
    records = [ln.record for ln in read_lines(told)]
    told.write_bytes(encode([dict(records[0], origin={"kind": "task-notification"}), records[1]]))
    old = claude_session(auto_homes)
    eight_days = time.time() - 8 * 86_400
    os.utime(old, (eight_days, eight_days))
    assert run("sync")[0] == 0
    assert pairs() == {}
    why = {path: entry[2] for path, entry in state().passed.items()}
    assert why == {
        str(scripted): "a script ran it",
        str(gone): "its folder is gone",
        str(ours): "duologue made it",
        str(moved): "it continues in another session",
        str(told): "nothing was typed in it yet",
    }  # and the one too old to look at is not there at all


def test_codex_threads_left_alone_and_why(auto_homes):
    n = iter(range(10, 99))
    made = {}

    def thread(why: str, **kwargs) -> None:
        tid = f"01a1ab4d-705e-7cc1-9b67-8e3e3f3f97{next(n)}"
        made[str(codex_thread(auto_homes, thread=tid, **kwargs))] = why

    thread("a script ran it", meta={"source": "exec"})
    thread("a script ran it", meta={"source": "mcp"})
    thread("a subagent thread", meta={"source": {"subagent": {}}})
    thread("duologue made it", meta={"originator": "duologue"})
    thread("it was rewound in Codex", meta={"history_base": {"rollout": "x"}})
    thread("its folder is gone", cwd="/no/such/folder")
    x = CodexRollout(THREAD_2)
    x.start("running")
    x.user_says("still going", "r-u")
    running = write_rollout(auto_homes.codex, THREAD_2, x.records, cwd=auto_homes.work)
    made[str(running)] = "no turn has finished in it yet"
    folder = auto_homes.codex / "sessions" / "2026" / "11" / "04"
    zst = folder / "rollout-2026-11-04T14-00-00-01a1ab4d-705e-7cc1-9b67-8e3e3f3f9799.jsonl.zst"
    zst.write_bytes(b"compressed")
    made[str(zst)] = "Codex compressed it while it was idle"
    assert run("sync")[0] == 0
    assert pairs() == {}
    assert {path: entry[2] for path, entry in state().passed.items()} == made


def test_scripted_runs_are_paired_once_the_user_asks(auto_homes):
    claude_session(auto_homes, entrypoint="sdk-cli")
    codex_thread(auto_homes, meta={"source": "exec"})
    assert run("sync")[0] == 0 and pairs() == {}
    set_config(auto_homes, "auto_scripted = true\n")
    assert run("sync")[0] == 0
    got = pairs().values()
    assert {p.claude for p in got} >= {SESSION} and {p.codex for p in got} >= {THREAD}


def test_auto_off_and_the_window(auto_homes):
    path = claude_session(auto_homes)
    set_config(auto_homes, "auto = false\n")
    code, out = run("sync")
    assert code == 0 and pairs() == {}, out
    cfg = auto_homes.duo / "config.toml"
    cfg.write_text(cfg.read_text().replace("auto = false\n", "auto_days = 0.5\n"))
    day = time.time() - 86_400
    os.utime(path, (day, day))
    assert run("sync")[0] == 0 and pairs() == {}
    cfg.write_text(cfg.read_text().replace("auto_days = 0.5\n", "auto_days = 2\n"))
    assert run("sync")[0] == 0 and [p.claude for p in pairs().values()] == [SESSION]


def test_syncing_named_pairs_pairs_nothing_new(both):
    claude_session(both.h, session=OTHER)
    code, out = run("sync", both.name)
    assert code == 0, out
    assert list(pairs()) == [both.name]


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
    assert SESSION not in state().ignored


def test_a_conversation_passed_over_is_looked_at_again_once_it_changes(auto_homes):
    path = claude_session(auto_homes)
    records = [ln.record for ln in read_lines(path)]
    path.write_bytes(encode([dict(records[0], isMeta=True)]))  # nothing typed yet
    assert run("sync")[0] == 0 and pairs() == {}
    assert state().passed[str(path)][2] == "nothing was typed in it yet"
    path.write_bytes(encode(records))
    assert run("sync")[0] == 0
    assert [p.claude for p in pairs().values()] == [SESSION]


def test_one_pass_tries_the_newest_first_and_at_most_per_pass(auto_homes, monkeypatch):
    monkeypatch.setattr(auto, "PER_PASS", 2)
    ids = [f"5e550000-0000-4000-8000-00000000010{n}" for n in range(3)]
    for k, sid in enumerate(ids):
        path = claude_session(auto_homes, session=sid)
        when = time.time() - 3600 * (3 - k)  # the last is the newest
        os.utime(path, (when, when))
    run("sync")
    assert sorted(p.claude for p in pairs().values()) == sorted(ids[1:])
    run("sync")
    assert len(pairs()) == 3


def test_a_failed_try_counts_toward_the_pass(auto_homes, monkeypatch):
    monkeypatch.setattr(auto, "PER_PASS", 1)
    claude_session(auto_homes, session=OTHER)
    claude_session(auto_homes)
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs.get("claude"))
        raise AppServerError("thread/resume", {"message": "no answer"})

    monkeypatch.setattr(linking, "link", fail)
    run("sync")
    assert len(calls) == 1


def test_a_try_that_failed_is_made_again_later_and_logged_once(auto_homes, monkeypatch):
    claude_session(auto_homes)
    real = linking.link
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise AppServerError("thread/resume", {"message": "Codex did not answer"})
        return real(*args, **kwargs)

    monkeypatch.setattr(linking, "link", flaky)
    assert run("sync")[0] == 0
    assert run("sync")[0] == 0  # too soon: not tried again
    assert len(calls) == 1 and pairs() == {}
    _, out = run("status")
    assert "not paired yet, tried again later: 1" in out
    e = engine()
    later = time.time() + auto.RETRY_AFTER + 1
    auto.link_new(e, now=later)  # fails again: not logged again
    assert pairs() == {}
    said = auto.link_new(engine(), now=later + auto.RETRY_AFTER + 1)
    assert said and [p.claude for p in pairs().values()] == [SESSION]
    log = (auto_homes.duo / "log.jsonl").read_text()
    assert log.count("Codex did not answer") == 1
    assert not state().failed


def test_a_conversation_refused_as_it_stands_is_passed_over_quietly(auto_homes, monkeypatch):
    claude_session(auto_homes)

    def refuse(*args, **kwargs):
        raise linking.LinkError("the Claude session has nothing to copy yet")

    monkeypatch.setattr(linking, "link", refuse)
    run("sync")
    assert reasons() == ["the Claude session has nothing to copy yet"]


def test_new_claude_sessions_wait_for_a_codex_duologue_knows(auto_homes, monkeypatch):
    claude_session(auto_homes)
    read = []
    real = auto.claude_reason
    with monkeypatch.context() as m:
        m.setattr(gate, "KNOWN_FORMATS", {})
        m.setattr(auto, "claude_reason", lambda *a: read.append(1) or real(*a))
        code, out = run("sync")
        assert code == 0 and pairs() == {} and not read  # not read while it cannot be paired
        _, out = run("status")
        assert "waiting to be paired: 1" in out and "has not been tested" in out
    (auto_homes.duo / "gate.json").unlink()
    assert run("sync")[0] == 0 and [p.claude for p in pairs().values()] == [SESSION]
    assert "waiting to be paired" not in run("status")[1]


def test_status_says_what_was_left_out(auto_homes):
    claude_session(auto_homes, entrypoint="sdk-cli")
    run("sync")
    _, out = run("status")
    assert "left out: 1 (1 a script ran it)" in out


def test_a_pipe_named_like_a_transcript_is_not_opened(auto_homes):
    import signal

    folder = auto_homes.claude / "projects" / project_folder(auto_homes.work)
    folder.mkdir(parents=True)
    os.mkfifo(folder / f"{OTHER}.jsonl")
    claude_session(auto_homes)

    def hung(*args):
        raise TimeoutError("the pass opened the pipe and hung")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)  # a failure, not a stuck run, if the pipe is ever opened
    try:
        assert run("sync")[0] == 0
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert [p.claude for p in pairs().values()] == [SESSION]


def test_an_unreadable_auto_json_stops_pairing_not_syncing(auto_homes, capsys):
    claude_session(auto_homes)
    (auto_homes.duo / "auto.json").write_text("{not json")
    code, out = run("sync")
    assert code == 1 and pairs() == {}
    assert "nothing paired by itself" in capsys.readouterr().err
    assert (auto_homes.duo / "auto.json").read_text() == "{not json"
    _, out = run("status")
    assert "nothing is paired by itself" in out
    code, _ = run("unlink", "anything")
    assert code == 1


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


def test_an_unlinked_session_is_not_paired_again_through_its_import(auto_homes):
    spath = claude_session(auto_homes)
    assert run("sync")[0] == 0
    [pair] = pairs().values()
    assert run("unlink", pair.name)[0] == 0
    imported(auto_homes, spath, thread=THREAD_2)  # found as a new Codex thread
    assert run("sync")[0] == 0
    assert pairs() == {}


def test_a_scripted_session_is_not_paired_through_its_import(auto_homes):
    spath = claude_session(auto_homes, entrypoint="sdk-cli")
    imported(auto_homes, spath)
    assert run("sync")[0] == 0
    assert pairs() == {}
    assert "its Claude session: a script ran it" in reasons()


def test_an_out_of_date_import_the_user_carried_on_in_is_paired_from_now_on(auto_homes):
    spath = claude_session(auto_homes)
    xpath = imported(auto_homes, spath)
    records = [ln.record for ln in read_lines(spath)]
    spath.write_bytes(encode([dict(records[0], timestamp="2026-11-04T14:00:09.000Z"), records[1]]))
    x = CodexRollout(THREAD, start_ms=1_793_900_000_000)
    x.start("codex-own-turn")
    x.user_says("carried on in codex", "own-u")
    x.agent_says("noted", "own-a")
    x.complete("noted")
    with open(xpath, "a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in x.records))
    resume(Codex(auto_homes.codex, auto_homes.bin), THREAD)
    code, out = run("sync")
    assert code == 0 and "turns from now on are copied both ways" in out, out
    [pair] = pairs().values()
    assert (pair.claude, pair.codex) == (SESSION, THREAD)


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
    assert reasons() == ["Codex's importer made it from a Claude session that is not free to pair"]


def test_everything_since_the_import_that_fits_is_copied_without_a_note(auto_homes):
    spath = claude_session(auto_homes)
    imported(auto_homes, spath)
    t = ClaudeTranscript(cwd=auto_homes.work)  # the end of a turn running at the import
    t._n, t._parent = 50, [ln.record for ln in read_lines(spath)][-1]["uuid"]
    t.assistant("and one more thing")
    with open(spath, "ab") as fh:
        fh.write(encode(t.records))
    add_claude_turn(auto_homes, spath, "next")
    code, out = run("sync")
    assert code == 0 and "left out" not in out, out
    rollout = Codex(auto_homes.codex, auto_homes.bin).rollout(THREAD).read_text()
    assert "next" in rollout and "are not copied here" not in rollout


def test_claude_turns_since_the_import_are_copied_within_the_budget(auto_homes):
    spath = claude_session(auto_homes)
    imported(auto_homes, spath)
    t = ClaudeTranscript(cwd=auto_homes.work)  # the end of a turn running at the import
    t._n, t._parent = 50, [ln.record for ln in read_lines(spath)][-1]["uuid"]
    t.assistant("x" * 300)
    with open(spath, "ab") as fh:
        fh.write(encode(t.records))
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
    return SimpleNamespace(claude=claude, codex=codex, name=p.name, h=auto_homes)


def test_turns_written_in_both_apps_are_all_kept(both):
    both.claude.turn("in claude", "ok")
    both.codex.turn("in codex", "ok")
    outcome, p = sync(both.name)
    assert outcome.state == "synced" and "meanwhile" in outcome.text, outcome.text
    assert not p.conflict
    rollout = both.codex.path.read_text()
    # The note sits between Codex's turn and Claude's: it speaks of both.
    assert rollout.index('"in codex"') < rollout.index(ALONGSIDE_CODEX) < rollout.index("in claude")
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


def test_an_interrupted_write_still_brings_codexs_turns_to_claude(both, monkeypatch):
    both.claude.turn("in claude", "ok")
    both.codex.turn("in codex", "ok")
    real = threads.finish
    monkeypatch.setattr(threads, "finish", lambda *a, **k: (_ for _ in ()).throw(OSError("cut")))
    outcome, p = sync(both.name)
    assert outcome.state == "error" and p.pending, outcome.text
    monkeypatch.setattr(threads, "finish", real)
    for _ in range(3):
        sync(both.name)
    assert prompts(both.claude.records())[-2:] == ["in claude", "in codex"]


def test_the_same_time_note_is_written_once_while_claude_stays_open(both, monkeypatch):
    from duologue import engine as engine_module

    both.codex.turn("in codex", "ok")
    both.claude.turn("in claude", "ok")
    with monkeypatch.context() as m:
        m.setattr(engine_module, "live_pid", lambda *a: 12345)  # Claude Code has it open
        for n in range(3):
            outcome, _ = sync(both.name)
            assert outcome.state == "synced", outcome.text
            both.claude.turn(f"more {n}", "ok")
    rollout = both.codex.path.read_text()
    assert rollout.count(ALONGSIDE_CODEX) == rollout.count(ALONGSIDE_CODEX[:40]) == 2  # 1 turn
    for _ in range(3):
        sync(both.name)
    assert prompts(both.claude.records())[-1] == "in codex"


def test_an_undo_in_codex_of_claudes_copied_turn_is_told_to_claude(both):
    both.claude.turn("in claude", "ok")
    both.codex.turn("in codex", "ok")
    assert sync(both.name)[0].state == "synced"  # Claude's turn now follows Codex's there
    both.codex.add([both.codex.rollback(1)])  # the user undid it in Codex
    outcome, _ = sync(both.name)
    assert outcome.state == "synced", outcome.text
    told = json.dumps(both.claude.records()[-6:], ensure_ascii=False)
    assert 'the turn(s) that began \\"in claude\\" were undone in Codex' in told
    assert prompts(both.claude.records())[-1] == "in codex"


def test_an_out_of_date_import_found_first_is_left_alone(auto_homes):
    spath = claude_session(auto_homes)
    xpath = imported(auto_homes, spath)
    records = [ln.record for ln in read_lines(spath)]
    spath.write_bytes(encode([dict(records[0], timestamp="2026-11-04T14:00:09.000Z"), records[1]]))
    hour = time.time() - 3600
    os.utime(spath, (hour, hour))  # the thread is the newer of the two
    assert xpath.stat().st_mtime > hour
    e = engine()
    first = auto.found(e, AutoState.load(auto_state_file(e.config)), time.time())[0]
    assert (first.side, first.id) == ("codex", THREAD)
    assert run("sync")[0] == 0
    assert state().passed[str(xpath)][2] == (
        "Codex's importer made it, and the Claude session changed since Codex imported it"
    )
    [pair] = pairs().values()
    assert pair.claude == SESSION and pair.codex != THREAD  # the session got its own thread


def test_an_unlink_that_cannot_be_recorded_keeps_the_pair(auto_homes):
    claude_session(auto_homes)
    assert run("sync")[0] == 0
    [pair] = pairs().values()
    (auto_homes.duo / "auto.json").unlink()
    (auto_homes.duo / "auto.json").mkdir()
    assert run("unlink", pair.name)[0] == 1
    assert list(pairs()) == [pair.name]  # not gone without its decision


def test_an_idle_pass_does_not_rewrite_the_pairs_file(both):
    sync(both.name)
    state = both.h.duo / "pairs.json"
    before = state.stat().st_mtime_ns
    time.sleep(0.01)
    assert sync(both.name, force=False)[0].state == "idle"
    assert state.stat().st_mtime_ns == before


def test_status_forgets_what_was_paired_since(auto_homes, monkeypatch):
    claude_session(auto_homes)

    def fail(*args, **kwargs):
        raise AppServerError("thread/resume", {"message": "no answer"})

    with monkeypatch.context() as m:
        m.setattr(linking, "link", fail)
        run("sync")
    assert state().failed
    assert run("link", "--claude", SESSION)[0] == 0
    run("sync")
    assert not state().failed
    assert "not paired yet" not in run("status")[1]


def test_an_import_rewound_in_codex_is_not_used(auto_homes):
    spath = claude_session(auto_homes)
    xpath = imported(auto_homes, spath)
    suffix = "_01a1ab4d-705e-7cc1-9b67-000000000001.jsonl"
    rewound = xpath.with_name(xpath.name.replace(".jsonl", suffix))
    lines = xpath.read_text().splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["history_base"] = {"rollout": str(xpath)}
    rewound.write_text("\n".join([json.dumps(meta), *lines[1:]]) + "\n")
    old = time.time() - 60
    os.utime(xpath, (old, old))
    assert Codex(auto_homes.codex, auto_homes.bin).rollout(THREAD) == rewound
    code, out = run("sync")
    assert code == 0 and "importer" not in out, out
    [pair] = pairs().values()
    assert pair.claude == SESSION and pair.codex != THREAD  # a thread of its own


def test_a_link_that_must_wait_is_tried_on_the_next_pass(auto_homes, monkeypatch):
    from duologue.journal import Wait

    claude_session(auto_homes)

    def busy(*args, **kwargs):
        raise linking.LinkError("Codex's thread locks are busy") from Wait("busy")

    with monkeypatch.context() as m:
        m.setattr(linking, "link", busy)
        run("sync")
    assert not state().failed and list(state().waiting.values()) == [
        "Codex's thread locks are busy"
    ]
    assert run("sync")[0] == 0 and [p.claude for p in pairs().values()] == [SESSION]
