import subprocess
import sys
from importlib.metadata import requires, version

import duologue


def test_version_comes_from_the_installed_metadata():
    assert duologue.__version__ == version("duologue")


def test_runtime_is_standard_library_only():
    # Extras (dev tools) are allowed; anything a plain install pulls in is not.
    runtime = [r for r in requires("duologue") or [] if "extra ==" not in r]
    assert runtime == []


def test_python_dash_m_runs_the_cli():
    result = subprocess.run(
        [sys.executable, "-m", "duologue", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"duologue {duologue.__version__}"


def test_derived_ids_are_told_apart_from_the_apps_own():
    import uuid

    from duologue.codex.rollout import uuid7
    from duologue.translate.common import is_derived_id, stable_id

    assert is_derived_id(str(stable_id("claude-turn", "k", "u")))
    assert not is_derived_id(uuid7(1_793_800_800_000))
    assert not is_derived_id(str(uuid.uuid4()))
    assert not is_derived_id("turn-1") and not is_derived_id(None)
