import json
import os
import plistlib
import threading
from pathlib import Path

import pytest

from duologue import config, gate
from duologue.pairs import ActivityLog, Busy, Cursor, Pair, Pending, Store, exclusive
from duologue.service import Service, launchd_files, systemd_files


# ------------------------------------------------------------------------------------- config
def test_defaults(tmp_path: Path):
    cfg = config.load({"DUOLOGUE_HOME": str(tmp_path)})
    assert cfg.home == tmp_path
    assert cfg.claude_home == Path("~/.claude").expanduser()
    assert cfg.codex_home == Path("~/.codex").expanduser()
    assert (cfg.quiet_seconds, cfg.copy_budget, cfg.codex_source) == (15.0, 400_000, "vscode")
    assert cfg.notify == () and not cfg.allow_untested_codex
    assert (cfg.auto, cfg.auto_days, cfg.auto_scripted, cfg.on_conflict) == (
        True,
        7.0,
        False,
        "keep-both",
    )


def test_the_apps_variables_then_the_config_file(tmp_path: Path):
    env = {"DUOLOGUE_HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "/c", "CODEX_HOME": "/x"}
    assert (config.load(env).claude_home, config.load(env).codex_home) == (Path("/c"), Path("/x"))
    (tmp_path / "config.toml").write_text(
        'claude_home = "/cc"\nnotify = ["say", "duologue"]\nquiet_seconds = 3\n'
    )
    cfg = config.load(env)
    assert cfg.claude_home == Path("/cc") and cfg.codex_home == Path("/x")
    assert cfg.notify == ("say", "duologue") and cfg.quiet_seconds == 3.0


@pytest.mark.parametrize(
    "text",
    [
        "nonsense = 1\n",
        'quiet_seconds = "soon"\n',
        "copy_budget = 0\n",
        "allow_untested_codex = 1\n",
        "notify = [1]\n",
        "not toml = =\n",
        "quiet_seconds = nan\n",
        "quiet_seconds = inf\n",
        "auto = 1\n",
        "auto_days = 0\n",
        "auto_days = nan\n",
        'on_conflict = "ask"\n',
    ],
)
def test_a_bad_config_file_is_reported(tmp_path: Path, text: str):
    (tmp_path / "config.toml").write_text(text)
    with pytest.raises(config.ConfigError):
        config.load({"DUOLOGUE_HOME": str(tmp_path)})


# -------------------------------------------------------------------------------------- state
def test_pairs_survive_a_round_trip(tmp_path: Path):
    store = Store(tmp_path / "pairs.json")
    assert store.load() == {}
    p = Pair(name="x", claude="c", codex="t", claude_cursor=Cursor(10, "d", "u"))
    p.pending = Pending("codex", "/p", 1, 2, "s", ["t1"], {"end": 3}, {})
    store.save({"x": p})
    back = store.load()["x"]
    assert back == p and back.key == "c/t" and back.state == "linked"
    assert oct((tmp_path / "pairs.json").stat().st_mode & 0o777) == "0o600"


def test_pair_states():
    p = Pair(name="x", claude="c", codex="t")
    p.conflict = True
    assert p.state == "conflict"
    p.blocked = "why"
    assert p.state == "blocked"
    p.hold = "because"
    assert p.state == "held"


@pytest.mark.parametrize(
    "doc",
    [
        {"version": 99, "pairs": []},
        [],
        {"version": 1, "pairs": None},
        {"version": 1, "pairs": [{}]},
    ],
)
def test_an_unreadable_state_file_is_refused(tmp_path: Path, doc):
    (tmp_path / "pairs.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        Store(tmp_path / "pairs.json").load()


def test_one_command_at_a_time(tmp_path: Path):
    with exclusive(tmp_path / "lock"):
        with pytest.raises(Busy), exclusive(tmp_path / "lock", wait=0.3):
            pass
    with exclusive(tmp_path / "lock", wait=0.3):
        pass


def test_activity_log(tmp_path: Path, monkeypatch):
    log = ActivityLog(tmp_path / "log.jsonl")
    log.write("a", "link", "linked")
    log.write("b", "sync", "copied")
    log.write(None, "notify-failed", "x")
    assert [e["pair"] for e in log.tail()] == ["a", "b", None]
    assert [e["text"] for e in log.tail(1)] == ["x"]
    assert [e["event"] for e in log.tail(pair="a")] == ["link"]
    monkeypatch.setattr("duologue.pairs.LOG_LIMIT", 10)
    log.write("c", "sync", "rotated")
    assert (tmp_path / "log.jsonl.1").exists()
    assert [e["pair"] for e in log.tail()][-1] == "c"
    log.path.chmod(0o644)
    log.write("d", "sync", "tightened")
    assert oct(log.path.stat().st_mode & 0o777) == "0o600"
    other = tmp_path / "other.txt"
    other.write_text("not the log")
    other.chmod(0o644)
    log.path.unlink()
    log.path.symlink_to(other)  # a link planted where the log goes is replaced, not followed
    log.write("e", "sync", "after the link")
    assert not log.path.is_symlink() and oct(other.stat().st_mode & 0o777) == "0o644"
    log.path.unlink()
    os.link(other, log.path)  # a hard link: writing through it would change the other file
    log.write("f", "sync", "after the hard link")
    assert other.read_text() == "not the log" and log.path.stat().st_nlink == 1
    log.path.unlink()
    os.mkfifo(log.path)  # a named pipe: opening it to write would wait for a reader
    # daemon: a regression that blocks fails this test instead of holding pytest open
    done = threading.Thread(target=log.write, args=("g", "sync", "after the pipe"), daemon=True)
    done.start()
    done.join(5)
    assert not done.is_alive() and log.path.is_file()


# --------------------------------------------------------------------------------------- gate
def test_version_tuple():
    assert gate.version_tuple("0.166.0-alpha.6") == (0, 166, 0)
    assert gate.version_tuple("2.1.318 (Claude Code)") == (2, 1, 318)
    assert gate.version_tuple(None) is None


def schema(folder: Path, variants: list[dict], extra: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"definitions": {"ThreadItem": {"oneOf": variants}, **(extra or {})}}
    (folder / "s.json").write_text(json.dumps(doc))
    return folder


def kind(name: str, **props) -> dict:
    return {"properties": {"type": {"enum": [name]}, **props}}


def test_fingerprint_covers_the_written_kinds_and_what_they_reference(tmp_path: Path):
    kinds = [kind(k) for k in gate.ITEM_KINDS[1:]]
    base = [kind("userMessage", phase={"$ref": "#/definitions/P"}), *kinds]
    one = gate.fingerprint(schema(tmp_path / "a", base, {"P": {"enum": ["x"]}}))
    other_reference = gate.fingerprint(schema(tmp_path / "b", base, {"P": {"enum": ["y"]}}))
    unrelated_kind = gate.fingerprint(
        schema(tmp_path / "c", [*base, kind("sleep")], {"P": {"enum": ["x"]}})
    )
    assert one and one != other_reference
    assert one == unrelated_kind  # a new kind duologue does not write changes nothing
    assert gate.fingerprint(schema(tmp_path / "d", kinds)) is None  # a written kind is missing
    assert gate.fingerprint(tmp_path / "empty") is None


# ------------------------------------------------------------------------------------ service
def test_launchd_agent(tmp_path: Path):
    env = {"PATH": "/opt/bin:/usr/bin", "CODEX_HOME": "/x", "SECRET_TOKEN": "no"}
    [f] = launchd_files(tmp_path, tmp_path / "logs", env)
    plist = plistlib.loads(f.content)
    assert f.path == tmp_path / "Library/LaunchAgents/duologue.sync.plist"
    assert plist["ProgramArguments"][-3:] == ["duologue", "sync", "--quiet"]
    assert plist["StartInterval"] == 30
    assert plist["EnvironmentVariables"] == {"PATH": "/opt/bin:/usr/bin", "CODEX_HOME": "/x"}
    assert plist["Umask"] == 0o077


def test_systemd_values_are_escaped_and_folders_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {"PATH": 'a"b\\c%d', "DUOLOGUE_HOME": "rel"}
    service, _ = systemd_files(tmp_path, env, tmp_path / "duo")
    text = service.content.decode()
    assert 'Environment="PATH=a\\"b\\\\c%%d"' in text
    assert f'Environment="DUOLOGUE_HOME={tmp_path / "rel"}"' in text


def test_systemd_timer(tmp_path: Path):
    service, timer = systemd_files(tmp_path, {"PATH": "/usr/bin"}, tmp_path / "duo")
    assert service.path.name == "duologue.service" and timer.path.name == "duologue.timer"
    assert b"Type=oneshot" in service.content and b'Environment="PATH=/usr/bin"' in service.content
    assert f"StandardOutput=append:{tmp_path / 'duo' / 'service.log'}".encode() in service.content
    assert b"UMask=0077" in service.content
    assert b"OnUnitActiveSec=30" in timer.content


class Calls:
    def __init__(self, fail: bool = False):
        self.argv: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.argv.append(argv)
        return type("R", (), {"returncode": 1 if self.fail else 0, "stdout": "", "stderr": "no"})()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_and_uninstall(tmp_path: Path, platform: str):
    calls = Calls()
    service = Service(tmp_path / "duo", env={"HOME": str(tmp_path), "PATH": "/usr/bin"}, run=calls)
    service.platform = platform
    assert service.status() == "not installed"
    written = service.install()
    assert all(Path(p).exists() for p in written)
    assert oct(service.log_file.stat().st_mode & 0o777) == "0o600"
    assert service.status() == "installed and running"
    first = calls.argv[0][0]
    assert first == ("launchctl" if platform == "darwin" else "systemctl")
    removed = service.uninstall()
    assert sorted(removed) == sorted(written) and not any(Path(p).exists() for p in written)


def test_a_failing_install_is_reported(tmp_path: Path):
    service = Service(tmp_path, env={"HOME": str(tmp_path)}, run=Calls(fail=True))
    service.platform = "linux"
    with pytest.raises(RuntimeError, match="failed"):
        service.install()


def test_no_service_elsewhere(tmp_path: Path):
    service = Service(tmp_path, env={"HOME": str(tmp_path)})
    service.platform = "win32"
    with pytest.raises(RuntimeError):
        service.files()


def test_auto_json_that_cannot_be_read_is_reported_not_reset(tmp_path: Path):
    from duologue.pairs import AutoState, AutoStateError

    path = tmp_path / "auto.json"
    for text in ("not json", "[1, 2]", '{"ignored": "x"}'):
        path.write_text(text)
        with pytest.raises(AutoStateError):
            AutoState.load(path)
        assert path.read_text() == text  # the user's unlinks are not thrown away
    path.write_text('{"ignored": ["a"], "passed": [], "settings": 3}')
    state = AutoState.load(path)
    assert (state.ignored, state.passed, state.settings) == ({"a"}, {}, {})
    state.ignore("b")
    state.unignore("a")
    assert AutoState.load(path).ignored == {"b"}


def test_a_pair_written_by_a_later_duologue_still_loads(tmp_path: Path):
    store = Store(tmp_path / "pairs.json")
    store.save({"x": Pair(name="x", claude="c", codex="t")})
    data = json.loads((tmp_path / "pairs.json").read_text())
    data["pairs"][0]["field_from_the_future"] = 1
    (tmp_path / "pairs.json").write_text(json.dumps(data))
    pairs = store.load()
    assert pairs["x"].claude == "c"
    store.save(pairs)  # and saving here keeps what it does not know
    saved = json.loads((tmp_path / "pairs.json").read_text())["pairs"][0]
    assert saved["field_from_the_future"] == 1


def test_auto_json_entries_that_are_not_well_formed_are_dropped(tmp_path: Path):
    from duologue.pairs import AutoState

    path = tmp_path / "auto.json"
    path.write_text(
        json.dumps(
            {
                "ignored": ["a"],
                "passed": {"/p": 5, "/q": [1, 2, "why"]},
                "failed": {"/r": "soon"},
                "waiting": {"x": 1},
                "from_the_future": {"k": 1},
            }
        )
    )
    state = AutoState.load(path)
    assert state.passed == {"/q": [1, 2, "why"]} and state.failed == {} and state.waiting == {}
    state.save()
    saved = json.loads(path.read_text())
    assert saved["from_the_future"] == {"k": 1}
    assert saved["passed"] == {"/q": [1, 2, "why"]} and saved["failed"] == saved["waiting"] == {}


def test_a_cursor_that_is_not_an_object_is_reported_not_a_traceback(tmp_path: Path):
    store = Store(tmp_path / "pairs.json")
    store.save({"x": Pair(name="x", claude="c", codex="t")})
    data = json.loads((tmp_path / "pairs.json").read_text())
    data["pairs"][0]["codex_cursor"] = 7
    (tmp_path / "pairs.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="a pair is not readable"):
        store.load()
