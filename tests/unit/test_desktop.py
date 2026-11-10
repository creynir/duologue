"""The Claude desktop app's session cards, in a throwaway folder laid out as the app lays it."""

import json
import stat
import uuid
from pathlib import Path

from duologue.claude import desktop

SESSION = "5e550000-0000-4000-8000-000000000001"
ACCOUNT = "acc00000-0000-4000-8000-000000000001"


def app_folder(tmp_path: Path, account: str = ACCOUNT, devices=("dev",)) -> Path:
    """A desktop app folder signed in to ``account``, with one card of the app's own per
    device (the last device's is the newest)."""
    folder = tmp_path / "Claude"
    (folder / desktop.SESSIONS).mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}))
    for n, device in enumerate(devices):
        d = folder / desktop.SESSIONS / account / device
        d.mkdir(parents=True)
        card = {"sessionId": "x", "model": f"model-{n}", "effort": "high"}
        card["permissionMode"] = "bypassPermissions"  # the user's choice for that session
        (d / f"local_{uuid.uuid4()}.json").write_text(json.dumps({**card, "secret": "no"}))
    return folder


def test_cards_go_where_the_app_lists_the_signed_in_account(tmp_path):
    folder = app_folder(tmp_path, devices=("old", "new"))
    other = folder / desktop.SESSIONS / "acc00000-0000-4000-8000-000000000002" / "dev"
    other.mkdir(parents=True)
    (other / f"local_{uuid.uuid4()}.json").write_text("{}")
    assert desktop.workspace(folder) == folder / desktop.SESSIONS / ACCOUNT / "new"


def test_never_another_accounts_list_nor_a_path_from_the_apps_config(tmp_path):
    folder = app_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "dev"  # a list a path in the app's config could reach
    elsewhere.mkdir(parents=True)
    (elsewhere / f"local_{uuid.uuid4()}.json").write_text("{}")
    missing = "acc00000-0000-4000-8000-000000000009"
    for account in (missing, "../../elsewhere", str(elsewhere.parent)):
        (folder / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}))
        assert desktop.workspace(folder) is None


def test_no_list_no_card(tmp_path):
    assert desktop.workspace(None) is None
    assert desktop.workspace(tmp_path) is None
    (tmp_path / desktop.SESSIONS).mkdir()
    assert (
        desktop.write_card(
            tmp_path, session_id=SESSION, cwd="/w", title="t", created_ms=1, last_ms=2, turns=1
        )
        is None
    )


def test_a_card_names_the_session_and_takes_the_apps_settings(tmp_path):
    folder = app_folder(tmp_path)
    card = desktop.write_card(
        folder,
        session_id=SESSION,
        cwd="/work",
        title="the build",
        created_ms=10,
        last_ms=20,
        turns=3,
    )
    data = json.loads(card.path.read_text())
    assert card.path.parent == folder / desktop.SESSIONS / ACCOUNT / "dev"
    assert data["sessionId"] == card.id == card.path.stem and card.id.startswith("local_")
    assert (data["cliSessionId"], data["cwd"], data["originCwd"]) == (SESSION, "/work", "/work")
    assert (data["model"], data["effort"]) == ("model-0", "high")
    assert data["permissionMode"] == "default"  # never another session's choice
    assert (data["title"], data["completedTurns"], data["isArchived"]) == ("the build", 3, False)
    assert (data["createdAt"], data["lastActivityAt"]) == (10, 20)
    assert "secret" not in data  # nothing else of the app's own cards
    assert stat.S_IMODE(card.path.stat().st_mode) == 0o600
    assert desktop.find(folder, card.id).session == SESSION


def test_find_takes_only_card_ids(tmp_path):
    folder = app_folder(tmp_path)
    assert desktop.find(folder, "../../config") is None
    assert desktop.find(folder, f"local_{uuid.uuid4()}") is None
