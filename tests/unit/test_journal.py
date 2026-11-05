import hashlib
from pathlib import Path

import pytest

from duologue.journal import Intent, MemoryJournal, prefix_hash, tail_state
from duologue.jsonl import read_lines


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


def test_a_write_followed_by_the_apps_own_lines(intent: Intent):
    intent.path.write_bytes(b"one\ntwo\nthree\nfour\n")
    assert tail_state(intent.path, intent) == "followed"


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


def test_a_new_file_never_made_is_absent(tmp_path: Path):
    intent = Intent("claude", tmp_path / "new.jsonl", 0, b"x\n", ("u",))
    assert tail_state(intent.path, intent) == "absent"


def test_intent_end_and_digest(intent: Intent):
    assert intent.end == 14
    assert intent.digest == hashlib.sha256(b"two\nthree\n").hexdigest()


def test_memory_journal_keeps_the_last_intent(intent: Intent):
    journal = MemoryJournal()
    journal.begin(intent)
    later = Intent("claude", intent.path, 0, b"x\n", ("v",))
    journal.begin(later)
    assert journal.intent is later


def test_a_rewrite_of_the_same_size_before_the_write_is_changed(intent: Intent):
    checked = Intent(
        intent.side,
        intent.path,
        intent.base,
        intent.data,
        intent.ids,
        prefix_hash(intent.path, intent.base),
    )
    assert tail_state(intent.path, checked) == "absent"
    intent.path.write_bytes(b"ONE\n")  # same size, other bytes
    assert tail_state(intent.path, checked) == "changed"


def test_reading_from_inside_a_line_is_refused(tmp_path: Path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    assert [ln.record for ln in read_lines(path, 9)] == [{"b": 2}]
    with pytest.raises(ValueError):
        read_lines(path, 3)


def test_clean_joins_surrogate_pairs_and_replaces_lone_ones():
    from duologue.jsonl import clean

    assert clean("plain") == "plain"
    assert clean("\ud83d\ude00 ok") == "\U0001f600 ok"  # a pair: the emoji it stands for
    assert clean("cut \ud83d") == "cut \ufffd"
    assert clean("\ude00 then \ud83d\ude00") == "\ufffd then \U0001f600"
