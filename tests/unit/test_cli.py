import pytest

from duologue import __version__
from duologue.cli import COMMANDS, NOT_IMPLEMENTED, main


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


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_planned_commands_say_they_are_not_implemented(name, capsys):
    assert main([name]) == NOT_IMPLEMENTED
    assert capsys.readouterr().err.strip() == f"duologue {name}: not implemented yet"


def test_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["no-such-command"])
    assert exit_.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
