"""Running ``duologue sync`` in the background: a launchd agent on macOS, a ``systemd --user``
timer on Linux.

The service runs with the environment the installing shell had for the things duologue needs
(``PATH``, so it finds ``claude``, ``codex`` and the ``node`` they run on, and the folders
settings), because launchd and systemd start jobs with an almost empty one.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LABEL = "duologue.sync"
UNIT = "duologue"
INTERVAL = 30  # seconds between passes
KEPT_ENV = ("PATH", "DUOLOGUE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "LANG")


@dataclass(frozen=True)
class ServiceFile:
    path: Path
    content: bytes


def command() -> list[str]:
    """How the service runs duologue: this interpreter and this installation."""
    return [sys.executable, "-m", "duologue", "sync", "--quiet"]


def environment(env: Mapping[str, str]) -> dict[str, str]:
    return {k: env[k] for k in KEPT_ENV if env.get(k)}


def launchd_files(home: Path, log_dir: Path, env: Mapping[str, str]) -> list[ServiceFile]:
    plist = {
        "Label": LABEL,
        "ProgramArguments": command(),
        "StartInterval": INTERVAL,
        "RunAtLoad": True,
        "EnvironmentVariables": environment(env),
        "StandardOutPath": str(log_dir / "service.log"),
        "StandardErrorPath": str(log_dir / "service.log"),
        "ProcessType": "Background",
    }
    path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return [ServiceFile(path, plistlib.dumps(plist))]


def systemd_files(home: Path, env: Mapping[str, str]) -> list[ServiceFile]:
    folder = home / ".config" / "systemd" / "user"
    lines = [
        "[Unit]",
        "Description=duologue: sync linked Claude Code and Codex conversations",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={shlex.join(command())}",
    ]
    lines += [f'Environment="{k}={v}"' for k, v in environment(env).items()]
    timer = [
        "[Unit]",
        "Description=Run duologue sync every 30 seconds",
        "",
        "[Timer]",
        "OnBootSec=30",
        f"OnUnitActiveSec={INTERVAL}",
        "AccuracySec=5",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return [
        ServiceFile(folder / f"{UNIT}.service", ("\n".join(lines) + "\n").encode()),
        ServiceFile(folder / f"{UNIT}.timer", ("\n".join(timer) + "\n").encode()),
    ]


class Service:
    """Install, remove and inspect the background service for this platform."""

    def __init__(self, log_dir: Path, env: Mapping[str, str] | None = None, run=subprocess.run):
        self.env = dict(os.environ if env is None else env)
        self.home = Path(self.env.get("HOME") or Path.home())
        self.log_dir = log_dir
        self.run = run
        self.platform = sys.platform

    def files(self) -> list[ServiceFile]:
        if self.platform == "darwin":
            return launchd_files(self.home, self.log_dir, self.env)
        if self.platform.startswith("linux"):
            return systemd_files(self.home, self.env)
        raise RuntimeError(f"no background service for {self.platform}; run duologue sync yourself")

    def install(self) -> list[str]:
        files = self.files()
        for f in files:
            f.path.parent.mkdir(parents=True, exist_ok=True)
            f.path.write_bytes(f.content)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            target = f"gui/{os.getuid()}"
            self._call(["launchctl", "bootout", f"{target}/{LABEL}"], check=False)
            self._call(["launchctl", "bootstrap", target, str(files[0].path)])
        else:
            self._call(["systemctl", "--user", "daemon-reload"])
            self._call(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
        return [str(f.path) for f in files]

    def uninstall(self) -> list[str]:
        files = self.files()
        if self.platform == "darwin":
            self._call(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
        else:
            self._call(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
        removed = []
        for f in files:
            if f.path.exists():
                f.path.unlink()
                removed.append(str(f.path))
        if self.platform != "darwin":
            self._call(["systemctl", "--user", "daemon-reload"], check=False)
        return removed

    def status(self) -> str:
        files = self.files()
        if not all(f.path.exists() for f in files):
            return "not installed"
        if self.platform == "darwin":
            r = self._call(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False)
        else:
            r = self._call(["systemctl", "--user", "is-active", f"{UNIT}.timer"], check=False)
        return "installed and running" if r.returncode == 0 else "installed, not running"

    def _call(self, argv: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
        r = self.run(list(argv), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed: {(r.stderr or r.stdout).strip()}")
        return r
