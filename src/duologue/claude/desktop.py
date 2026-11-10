"""The Claude desktop app's list of Claude Code sessions.

The desktop app lists only the sessions it has a card for, a small JSON file per session at
``<data>/claude-code-sessions/<account>/<device>/local_<uuid>.json`` that names the transcript
(``cliSessionId``) and the folder it groups the session under (``cwd``). ``<data>`` is the
app's own folder (``~/Library/Application Support/Claude`` on macOS). The account is the one the
app is signed in to (``lastKnownAccountUuid`` in its ``config.json``); the device folder is the
one with the newest card in that account.

duologue writes a card for each Claude session it creates, with the app's model, effort and
permission settings taken from the newest card there. The app keeps its list in memory while it
runs, so a card written meanwhile shows the next time it starts.

When the user carries on in such a session in the desktop app, the app copies it into a session
of its own and points the card there (the copy keeps every record, ids included). duologue
follows the card: the pair goes on with the copy.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from duologue.ids import UUID
from duologue.pairs import write_atomic

SESSIONS = "claude-code-sessions"
CARD = "local_"
FROM_TEMPLATE = ("model", "effort", "permissionMode")


def default_folder() -> Path | None:
    """The desktop app's folder on this system, or None where it has none."""
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Claude").expanduser()
    return None


@dataclass(frozen=True)
class Card:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return self.path.stem

    @property
    def session(self) -> str | None:
        value = self.data.get("cliSessionId")
        return value if isinstance(value, str) and UUID.fullmatch(value) else None


def _read(path: Path) -> dict | None:
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def workspace(folder: Path | None) -> Path | None:
    """The folder the app lists the signed-in account's sessions from: the device folder with
    the newest card. None when the app has none (it was never used for Claude Code)."""
    if folder is None or not (folder / SESSIONS).is_dir():
        return None
    account = (_read(folder / "config.json") or {}).get("lastKnownAccountUuid")
    accounts = [folder / SESSIONS / account] if isinstance(account, str) and account else []
    accounts = [a for a in accounts if a.is_dir()] or [
        a for a in (folder / SESSIONS).iterdir() if a.is_dir()
    ]
    newest: tuple[float, Path] | None = None
    for acct in accounts:
        for device in acct.iterdir():
            if not device.is_dir():
                continue
            for card in device.glob(f"{CARD}*.json"):
                try:
                    when = card.stat().st_mtime
                except OSError:
                    continue
                if newest is None or when > newest[0]:
                    newest = (when, device)
    return newest[1] if newest else None


def find(folder: Path | None, card_id: str) -> Card | None:
    """The card ``card_id`` (its file name without ``.json``), wherever the app keeps it."""
    if folder is None or not card_id.startswith(CARD) or not UUID.fullmatch(card_id[len(CARD) :]):
        return None
    for path in (folder / SESSIONS).glob(f"*/*/{card_id}.json"):
        data = _read(path)
        if data is not None:
            return Card(path, data)
    return None


def write_card(
    folder: Path | None,
    *,
    session_id: str,
    cwd: str,
    title: str,
    created_ms: int,
    last_ms: int,
    turns: int,
) -> Card | None:
    """List the Claude session ``session_id`` in the desktop app. None when the app keeps no
    list here."""
    device = workspace(folder)
    if device is None:
        return None
    cards = sorted(device.glob(f"{CARD}*.json"), key=lambda p: p.stat().st_mtime)
    template = next((d for d in (_read(p) for p in reversed(cards)) if d), {})
    card_id = f"{CARD}{uuid.uuid4()}"
    data = {
        "sessionId": card_id,
        "cliSessionId": session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": created_ms,
        "lastActivityAt": last_ms,
        "lastFocusedAt": last_ms,
        **{k: template[k] for k in FROM_TEMPLATE if k in template},
        "isArchived": False,
        "title": title,
        "titleSource": "auto",
        "completedTurns": turns,
        "bridgeSessionIds": [],
        "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [],
        "classifierSummaryEnabled": True,
        "spawnSeed": {},
    }
    path = device / f"{card_id}.json"
    write_atomic(path, json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
    return Card(path, data)
