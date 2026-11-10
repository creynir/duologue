"""duologue's settings: where each app keeps its conversations, and how syncing behaves.

Settings come from ``<DUOLOGUE_HOME>/config.toml`` (``~/.duologue`` by default), then the apps' own
environment variables (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``), then defaults. Every key is
optional::

    claude_home = "~/.claude"      # Claude Code's folder
    codex_home = "~/.codex"        # Codex's folder
    claude_bin = "claude"
    codex_bin = "codex"
    quiet_seconds = 15             # a finished Claude turn waits this long before it is copied
    copy_budget = 400000           # characters of history copied into a side duologue creates
    codex_source = "vscode"        # how threads duologue creates are listed (the app lists vscode)
    notify = ["notify-send", "duologue"]   # run with a message as its last argument
    allow_untested_codex = false   # write to a Codex whose item format duologue has not seen
    auto = true                    # pair new conversations by itself (false: only what you link)
    auto_days = 7                  # ... ones that changed within this many days
    auto_scripted = false          # ... and scripted runs (claude -p, the Agent SDK, codex exec)
    on_conflict = "keep-both"      # after you wrote in both apps; "stop" waits for resolve
"""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """The config file is unreadable or holds a wrong key or value."""


@dataclass(frozen=True)
class Config:
    home: Path
    claude_home: Path
    codex_home: Path
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    quiet_seconds: float = 15.0
    copy_budget: int = 400_000
    codex_source: str = "vscode"
    notify: tuple[str, ...] = field(default_factory=tuple)
    allow_untested_codex: bool = False
    auto: bool = True
    auto_days: float = 7.0
    auto_scripted: bool = False
    on_conflict: str = "keep-both"

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def state_file(self) -> Path:
        return self.home / "pairs.json"

    @property
    def log_file(self) -> Path:
        return self.home / "log.jsonl"

    @property
    def journal_dir(self) -> Path:
        return self.home / "journal"

    @property
    def outputs_dir(self) -> Path:
        return self.home / "outputs"

    @property
    def lock_file(self) -> Path:
        return self.home / "lock"


_TYPES: dict[str, tuple[type, ...]] = {
    "claude_home": (str,),
    "codex_home": (str,),
    "claude_bin": (str,),
    "codex_bin": (str,),
    "quiet_seconds": (int, float),
    "copy_budget": (int,),
    "codex_source": (str,),
    "notify": (list,),
    "allow_untested_codex": (bool,),
    "auto": (bool,),
    "auto_days": (int, float),
    "auto_scripted": (bool,),
    "on_conflict": (str,),
}
CONFLICT_POLICIES = ("keep-both", "stop")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = Path(env.get("DUOLOGUE_HOME") or "~/.duologue").expanduser()
    data = _read(home / "config.toml")
    claude_home = data.get("claude_home") or env.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    codex_home = data.get("codex_home") or env.get("CODEX_HOME") or "~/.codex"
    notify = data.get("notify", [])
    if not all(isinstance(x, str) for x in notify):
        raise ConfigError("notify must be a list of strings: the command and its arguments")
    return Config(
        home=home,
        claude_home=Path(claude_home).expanduser(),
        codex_home=Path(codex_home).expanduser(),
        claude_bin=data.get("claude_bin", "claude"),
        codex_bin=data.get("codex_bin", "codex"),
        quiet_seconds=float(data.get("quiet_seconds", 15.0)),
        copy_budget=data.get("copy_budget", 400_000),
        codex_source=data.get("codex_source", "vscode"),
        notify=tuple(notify),
        allow_untested_codex=data.get("allow_untested_codex", False),
        auto=data.get("auto", True),
        auto_days=float(data.get("auto_days", 7.0)),
        auto_scripted=data.get("auto_scripted", False),
        on_conflict=data.get("on_conflict", "keep-both"),
    )


def _read(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    for key, value in data.items():
        if key not in _TYPES:
            raise ConfigError(f"{path}: unknown setting {key!r}")
        if not isinstance(value, _TYPES[key]) or (
            isinstance(value, bool) and bool not in _TYPES[key]
        ):
            raise ConfigError(f"{path}: {key} has the wrong type")
    quiet = data.get("quiet_seconds", 0)
    if not math.isfinite(quiet) or quiet < 0 or data.get("copy_budget", 1) <= 0:
        raise ConfigError(f"{path}: quiet_seconds must be 0 or more, copy_budget above 0")
    days = data.get("auto_days", 1)
    if not math.isfinite(days) or days <= 0:
        raise ConfigError(f"{path}: auto_days must be above 0")
    if data.get("on_conflict", "keep-both") not in CONFLICT_POLICIES:
        raise ConfigError(f"{path}: on_conflict must be " + " or ".join(CONFLICT_POLICIES))
    return data
