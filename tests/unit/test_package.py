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
