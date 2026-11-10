"""The commands, run through main() against the fake codex and throwaway homes."""

import json

import pytest
from builders import SESSION
from test_engine import THREAD, Claude, CodexThread, homes  # noqa: F401 (fixture)

from duologue.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def linked(homes, capsys):  # noqa: F811
    claude = Claude(homes.claude)
    claude.turn("first", "done")
    codex = CodexThread(homes.codex, homes.bin)
    assert main(["link", "--claude", SESSION, "--codex", THREAD, "--name", "work"]) == 0
    out = capsys.readouterr().out
    assert '"work": linked' in out
    return claude, codex


def test_status_sync_log(linked, capsys):
    claude, codex = linked
    assert main(["status"]) == 0
    assert '"work"  linked' in capsys.readouterr().out
    claude.turn("second", "ok")
    assert main(["sync"]) == 0
    assert "copied 1 Claude turn(s) to Codex" in capsys.readouterr().out
    assert main(["sync", "--quiet"]) == 0
    assert capsys.readouterr().out == ""  # nothing changed: nothing to say
    assert main(["log", "-n", "5"]) == 0
    log = capsys.readouterr().out
    assert '"work" link:' in log and '"work" to_codex:' in log
    assert main(["status", "--json"]) == 0
    [pair] = json.loads(capsys.readouterr().out)
    assert pair["copied"]["to_codex"] == 1


def test_hold_release_unlink(linked, capsys):
    assert main(["hold", "work", "--reason", "trying something"]) == 0
    assert main(["sync"]) == 0
    assert "held: on hold: trying something" in capsys.readouterr().out
    assert main(["release", "wo"]) == 0
    assert "taken off hold" in capsys.readouterr().out
    assert main(["unlink", "work"]) == 0
    assert main(["status"]) == 0
    assert "no pairs yet" in capsys.readouterr().out


def test_a_conflict_is_reported_and_resolved(linked, capsys):
    claude, codex = linked
    claude.turn("in claude", "ok")
    codex.turn("in codex", "ok")
    assert main(["sync"]) == 1
    assert "conflict" in capsys.readouterr().out
    assert main(["resolve", "work", "--keep", "codex"]) == 0
    assert main(["sync"]) == 0
    assert "Codex turn(s) to Claude" in capsys.readouterr().out


def test_errors_are_reported_with_the_command(linked, capsys):
    assert main(["release", "nothing"]) == 1
    assert "duologue release: no pair 'nothing'" in capsys.readouterr().err
    assert main(["link", "--claude", "nonsense"]) == 1
    assert "no Claude Code session" in capsys.readouterr().err


def test_doctor(linked, capsys):
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert "Codex 0.166.0" in out and "item format known (the fake codex)" in out
    assert 'pair "work": linked' in out
    assert "Claude Code not found" in out  # the homes point claude_bin at nothing
    assert code == 1


def test_doctor_looks_at_each_pairs_files(linked, capsys, monkeypatch):
    claude, codex = linked
    from duologue import cli

    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "MiB" not in out and "indexed" not in out  # nothing to say about either file
    monkeypatch.setattr(cli, "CLAUDE_FULL_LOAD", 100)
    with open(codex.path, "a") as fh:  # a line Codex has not indexed yet
        fh.write(json.dumps({"timestamp": "t", "ordinal": 99, "type": "event_msg", "payload": {}}))
        fh.write("\n")
    main(["doctor"])
    out = capsys.readouterr().out
    assert 'pair "work": the Claude transcript is 0.0 MiB; above 5 MiB' in out
    assert '    pair "work": Codex has indexed' in out  # a note, not a problem
    data = codex.path.read_bytes()
    codex.path.write_bytes(data[:200])  # shorter than what Codex indexed
    main(["doctor"])
    assert 'NO  pair "work": Codex\'s index of the thread ends at byte' in capsys.readouterr().out


def test_a_wait_is_one_line_not_a_traceback(linked, capsys, monkeypatch):
    claude, _ = linked
    other = "5e550000-0000-4000-8000-0000000000aa"
    claude.path.with_name(f"{other}.jsonl").write_bytes(claude.path.read_bytes())
    monkeypatch.setenv("FAKE_CODEX_SCHEMA", "changed")
    assert main(["link", "--claude", other]) == 1
    err = capsys.readouterr().err
    assert err.startswith("duologue link: not now: Codex 0.166.0") and "Traceback" not in err


def test_a_block_is_one_line_not_a_traceback(linked, capsys):
    claude, _ = linked
    claude.path.unlink()
    assert main(["sync"]) == 1
    assert "not on this machine" in capsys.readouterr().out
    assert main(["release", "work"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("duologue release: the Claude session") and "Traceback" not in err


def test_log_reports_an_ambiguous_pair(linked, capsys):
    from duologue import config
    from duologue.pairs import Pair, Store

    store = Store(config.load().state_file)
    pairs = store.load()
    pairs["workshop"] = Pair(
        name="workshop",
        claude="5e550000-0000-4000-8000-0000000000bb",
        codex="01a1ab4d-0000-7000-8000-0000000000bb",
    )
    store.save(pairs)
    assert main(["log", "wo"]) == 1
    assert "more than one pair" in capsys.readouterr().err
    assert main(["log", "work"]) == 0  # an exact name is never ambiguous


def test_hold_with_an_empty_reason_still_holds(linked, capsys):
    assert main(["hold", "work", "--reason", "   "]) == 0
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    [pair] = json.loads(capsys.readouterr().out)
    assert pair["hold"] == "held by you"
