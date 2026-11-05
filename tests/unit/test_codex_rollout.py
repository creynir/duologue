import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path

from builders import CodexRollout
from conftest import write_rollout

from duologue.codex.history import read_index
from duologue.codex.rollout import (
    find_rollout,
    history_mode,
    model_provider,
    new_rollout_path,
    open_turn,
    prefix_hash,
    session_meta,
    turn_ends,
    uuid7,
)
from duologue.jsonl import read_lines

THREAD = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f96ef"


def one_turn(turn_id: str = "turn-1") -> list[dict]:
    x = CodexRollout(THREAD)
    x.start(turn_id)
    x.user_says("hello", "u1")
    x.agent_says("hi", "a1")
    x.complete("hi")
    return x.records


def test_find_rollout_by_file_name(tmp_path: Path):
    path = write_rollout(tmp_path, THREAD, one_turn())
    assert find_rollout(tmp_path, THREAD) == path
    assert find_rollout(tmp_path, "01a1ab4d-0000-7000-8000-000000000000") is None


def test_find_rollout_in_the_archive_and_compressed(tmp_path: Path):
    folder = tmp_path / "archived_sessions"
    folder.mkdir()
    path = folder / f"rollout-2026-11-04T14-00-00-{THREAD}.jsonl.zst"
    path.write_bytes(b"")
    assert find_rollout(tmp_path, THREAD) == path


def test_find_rollout_prefers_the_path_codex_records(tmp_path: Path):
    write_rollout(tmp_path, THREAD, one_turn())
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("{}\n")
    con = sqlite3.connect(tmp_path / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text)")
    con.execute("insert into threads values (?, ?)", (THREAD, str(elsewhere)))
    con.commit()
    con.close()
    assert find_rollout(tmp_path, THREAD) == elsewhere


def test_a_recorded_path_that_is_gone_falls_back_to_the_files(tmp_path: Path):
    path = write_rollout(tmp_path, THREAD, one_turn())
    con = sqlite3.connect(tmp_path / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text)")
    con.execute("insert into threads values (?, ?)", (THREAD, str(tmp_path / "gone.jsonl")))
    con.commit()
    con.close()
    assert find_rollout(tmp_path, THREAD) == path


def test_meta_and_history_mode(tmp_path: Path):
    path = write_rollout(tmp_path, THREAD, one_turn(), history_mode="legacy")
    assert session_meta(path)["id"] == THREAD
    assert history_mode(path) == "legacy"
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    assert session_meta(bad) == {}
    assert history_mode(bad) is None


def test_read_lines_leaves_out_a_line_still_being_written(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    path.write_bytes(b'{"a": 1}\nnot json\n{"b": 2}\n{"c": ')
    lines = read_lines(path)
    assert [ln.record for ln in lines] == [{"a": 1}, {"b": 2}]
    assert lines[-1].end == len(b'{"a": 1}\nnot json\n{"b": 2}\n')
    assert [ln.record for ln in read_lines(path, lines[0].end)] == [{"b": 2}]
    assert read_lines(path, 0, lines[0].end) == lines[:1]


def test_turn_ends_and_open_turns(tmp_path: Path):
    records = one_turn("turn-1") + one_turn("turn-2")[:2]  # the second has not finished
    path = write_rollout(tmp_path, THREAD, records)
    lines = read_lines(path)
    ends = turn_ends(lines)
    assert [t for t, _ in ends] == ["turn-1"]
    assert ends[0][1] == lines[4].end
    assert open_turn(lines)
    assert not open_turn(lines[:5])


def test_an_aborted_turn_ends_too(tmp_path: Path):
    x = CodexRollout(THREAD)
    x.start("turn-1")
    x._line("event_msg", {"type": "turn_aborted", "turn_id": "turn-1", "reason": "interrupted"})
    lines = read_lines(write_rollout(tmp_path, THREAD, x.records))
    assert [t for t, _ in turn_ends(lines)] == ["turn-1"]
    assert not open_turn(lines)


def test_prefix_hash(tmp_path: Path):
    path = tmp_path / "f"
    path.write_bytes(b"abcdef")
    assert prefix_hash(path, 3) == hashlib.sha256(b"abc").hexdigest()
    assert prefix_hash(path, 100) == hashlib.sha256(b"abcdef").hexdigest()


def test_model_provider(tmp_path: Path):
    assert model_provider(tmp_path) == "openai"
    (tmp_path / "config.toml").write_text('model_provider = "local"\n')
    assert model_provider(tmp_path) == "local"
    (tmp_path / "config.toml").write_text("not toml = = \n")
    assert model_provider(tmp_path) == "openai"


def test_uuid7_is_time_ordered():
    ms = int(time.time() * 1000)
    a, b = uuid.UUID(uuid7(ms)), uuid.UUID(uuid7(ms + 1))
    assert a.version == 7 and a.variant == uuid.RFC_4122
    assert a < b
    assert int(a.hex[:12], 16) == ms


def test_new_rollout_path_is_dated_by_local_time(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        path = new_rollout_path(tmp_path, THREAD, 1_793_800_800_000)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert path == tmp_path / "sessions/2026/11/04" / f"rollout-2026-11-04T14-00-00-{THREAD}.jsonl"


def test_read_index(tmp_path: Path):
    assert read_index(tmp_path, THREAD).next_offset is None
    db = tmp_path / "thread_history_1.sqlite"
    sqlite3.connect(db).close()
    assert read_index(tmp_path, THREAD).turns == {}  # no tables yet
    con = sqlite3.connect(db)
    con.executescript(
        "create table thread_turns "
        "(thread_id text, turn_id text, rollout_ordinal int, status text);"
        "create table thread_history_projection_state (thread_id text, "
        "next_rollout_byte_offset int, next_rollout_ordinal int);"
    )
    con.execute("insert into thread_turns values (?, 'b', 9, 'completed')", (THREAD,))
    con.execute("insert into thread_turns values (?, 'a', 1, 'completed')", (THREAD,))
    con.commit()
    assert read_index(tmp_path, THREAD).turns == {"a": "completed", "b": "completed"}
    assert list(read_index(tmp_path, THREAD).turns) == ["a", "b"]
    assert read_index(tmp_path, THREAD).next_offset is None
    con.execute("insert into thread_history_projection_state values (?, 120, 10)", (THREAD,))
    con.commit()
    con.close()
    index = read_index(tmp_path, THREAD)
    assert (index.next_offset, index.next_ordinal) == (120, 10)


def test_read_index_never_writes(tmp_path: Path):
    db = tmp_path / "thread_history_1.sqlite"
    sqlite3.connect(db).close()
    before = os.stat(db).st_mtime_ns, db.read_bytes()
    read_index(tmp_path, THREAD)
    assert (os.stat(db).st_mtime_ns, db.read_bytes()) == before
