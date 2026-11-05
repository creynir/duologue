from pathlib import Path

import pytest

from duologue.journal import Intent, MemoryJournal, tail_state


@pytest.fixture
def intent(tmp_path: Path) -> Intent:
    path = tmp_path / "file.jsonl"
    path.write_bytes(b"one\n")
    return Intent("codex", path, 4, b"two\nthree\n", ("t1",))


def test_nothing_written_is_absent(intent: Intent):
    assert tail_state(intent.path, intent) == "absent"


def test_a_cut_off_write_is_partial(intent: Intent):
    intent.path.write_bytes(b"one\ntwo\nth")
    assert tail_state(intent.path, intent) == "partial"


def test_the_whole_write_is_complete(intent: Intent):
    intent.path.write_bytes(b"one\ntwo\nthree\n")
    assert tail_state(intent.path, intent) == "complete"


def test_a_write_followed_by_the_apps_own_lines_is_complete(intent: Intent):
    intent.path.write_bytes(b"one\ntwo\nthree\nfour\n")
    assert tail_state(intent.path, intent) == "complete"


@pytest.mark.parametrize(
    "content",
    [b"one\nTWO\nthree\n", b"on", b"one\nfour\n"],
    ids=["different bytes", "shorter than the base", "someone else's line"],
)
def test_anything_else_is_changed(intent: Intent, content: bytes):
    intent.path.write_bytes(content)
    assert tail_state(intent.path, intent) == "changed"


def test_a_missing_file_is_changed(intent: Intent):
    intent.path.unlink()
    assert tail_state(intent.path, intent) == "changed"


def test_intent_end_and_digest(intent: Intent):
    assert intent.end == 14
    assert len(intent.digest) == 64


def test_memory_journal_keeps_the_last_intent(intent: Intent):
    journal = MemoryJournal()
    journal.begin(intent)
    assert journal.intent is intent
