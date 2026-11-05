import pytest

from duologue import __version__
from duologue.cli import COMMANDS, HANDLERS, main


def test_version_flag_prints_the_version(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.strip() == f"duologue {__version__}"


def test_no_command_prints_help_with_every_command(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_every_command_has_a_handler():
    assert set(HANDLERS) == set(COMMANDS)


def test_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["no-such-command"])
    assert exit_.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_resolve_needs_a_side(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["resolve", "x"])
    assert exit_.value.code == 2


def test_a_bad_config_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DUOLOGUE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("nonsense = 1\n")
    assert main(["status"]) == 1
    assert "unknown setting" in capsys.readouterr().err
