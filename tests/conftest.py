"""Fixtures shared by the unit and integration tests."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from builders import CWD, START_MS

from duologue.codex.threads import Codex
from duologue.translate.common import iso_ms

FAKE_CODEX = Path(__file__).parent / "fakes" / "fake_codex.py"


@pytest.fixture
def fake_codex_bin(tmp_path: Path) -> Path:
    """An executable that runs the fake codex with this interpreter."""
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def codex(tmp_path: Path, fake_codex_bin: Path, monkeypatch: pytest.MonkeyPatch) -> Codex:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.delenv("FAKE_CODEX_NO_INDEX", raising=False)
    return Codex(home=home, bin=str(fake_codex_bin), timeout=30)


def write_rollout(
    codex_home: Path,
    thread_id: str,
    records: list[dict],
    *,
    history_mode: str = "paginated",
    cwd: str = CWD,
) -> Path:
    """A rollout for ``thread_id``: session_meta, then ``records``, ordinals in sequence (none
    in a legacy rollout)."""
    folder = codex_home / "sessions" / "2026" / "11" / "04"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-04T14-00-00-{thread_id}.jsonl"
    meta = {
        "timestamp": iso_ms(START_MS),
        "type": "session_meta",
        "payload": {"id": thread_id, "cwd": cwd, "history_mode": history_mode},
    }
    lines = []
    for n, r in enumerate([meta, *records]):
        lines.append(dict(r, ordinal=n) if history_mode == "paginated" else r)
    path.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))
    return path
