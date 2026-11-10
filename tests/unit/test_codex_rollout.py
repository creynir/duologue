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
    session_meta,
    sqlite_home,
    turn_ends,
    uuid7,
)
from duologue.journal import prefix_hash
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


def test_find_rollout_prefers_a_readable_copy_to_a_compressed_one(tmp_path: Path):
    path = write_rollout(tmp_path, THREAD, one_turn())
    zst = path.with_name(path.name + ".zst")
    zst.write_bytes(b"")
    later = time.time() + 60
    os.utime(zst, (later, later))
    assert find_rollout(tmp_path, THREAD) == path


def test_find_rollout_finds_the_file_a_rewound_thread_continues_in(tmp_path: Path):
    path = write_rollout(tmp_path, THREAD, one_turn())
    rewound = path.with_name(
        path.name.replace(".jsonl", "_01a1ab4d-0000-7000-8000-00000000000b.jsonl")
    )
    rewound.write_text(path.read_text())
    later = time.time() + 60
    os.utime(rewound, (later, later))
    assert find_rollout(tmp_path, THREAD) == rewound
    other = "01a1ab4d-705e-7cc1-9b67-8e3e3f3f96e0"
    assert find_rollout(tmp_path, other) is None  # another thread's prefix does not match


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
    (tmp_path / "config.toml").write_text(
        'model_provider = "local"\nprofile = "work"\n[profiles.work]\nmodel_provider = "azure"\n'
    )
    assert model_provider(tmp_path) == "azure"
    (tmp_path / "config.toml").write_text(
        'model_provider = "local"\nprofile = "work"\n[profiles.work]\nmodel_provider = ""\n'
    )
    assert model_provider(tmp_path) == "local"
    (tmp_path / "config.toml").write_text("not toml = = \n")
    assert model_provider(tmp_path) is None


def test_uuid7_is_time_ordered():
    ms = int(time.time() * 1000)
    a, b = uuid.UUID(uuid7(ms)), uuid.UUID(uuid7(ms + 1))
    assert a.version == 7 and a.variant == uuid.RFC_4122
    assert a < b
    assert int(a.hex[:12], 16) == ms


def test_new_rollout_path_is_dated_by_local_time(tmp_path: Path):
    ms = 1_793_800_800_000
    local = time.localtime(ms / 1000)
    day, stamp = time.strftime("%Y/%m/%d", local), time.strftime("%Y-%m-%dT%H-%M-%S", local)
    path = new_rollout_path(tmp_path, THREAD, ms)
    assert path == tmp_path / "sessions" / day / f"rollout-{stamp}-{THREAD}.jsonl"


def test_sqlite_home_follows_codex(tmp_path: Path):
    assert sqlite_home(tmp_path, {}) == tmp_path
    assert sqlite_home(tmp_path, {"CODEX_SQLITE_HOME": "/elsewhere"}) == Path("/elsewhere")
    (tmp_path / "config.toml").write_text('sqlite_home = "/configured"\n')
    assert sqlite_home(tmp_path, {"CODEX_SQLITE_HOME": "/elsewhere"}) == Path("/configured")


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


# ------------------------------------------------------------------------- pre-flight checks
def translated(turns: int = 2):
    from builders import ClaudeTranscript

    from duologue.translate.claude_to_codex import claude_to_codex
    from duologue.translate.outputs import OutputStore

    c = ClaudeTranscript()
    for n in range(turns):
        c.user(f"q{n}")
        c.call("Bash", {"command": "ls"}, "a.py")
        c.assistant(f"a{n}")
    return claude_to_codex(
        c.records, thread_id=THREAD, key="k", outputs=OutputStore("/tmp/unused"), default_cwd="/w"
    )


def test_translated_turns_pass_the_structure_check():
    from duologue.codex.threads import check_turns

    assert check_turns(translated(), THREAD) == []


def test_the_structure_check_names_what_is_wrong():
    from dataclasses import replace

    from duologue.codex.threads import check_turns

    lines = translated()
    ends = [n for n, ln in enumerate(lines) if ln.payload.get("type") == "task_complete"]
    items = [n for n, ln in enumerate(lines) if ln.payload.get("type") == "item_completed"]

    cut = lines[: ends[0]] + lines[ends[0] + 1 :]  # the first turn never ends
    assert "before the last one ended" in check_turns(cut, THREAD)[0]
    assert check_turns(lines[:-1], THREAD) == ["the last turn does not end"]
    headless = lines[ends[0] + 2 :]  # the second turn without its task_started line
    assert "outside a turn" in check_turns(headless, THREAD)[0]
    twice = lines[: items[0] + 1] + [lines[items[0]]] + lines[items[0] + 1 :]
    assert "repeated id" in check_turns(twice, THREAD)[0]
    elsewhere = list(lines)
    p = dict(lines[items[0]].payload, thread_id="01a1ab4d-0000-7000-8000-00000000000c")
    elsewhere[items[0]] = replace(lines[items[0]], payload=p)
    assert "another turn or thread" in check_turns(elsewhere, THREAD)[0]


def test_a_rollout_codex_compressed_in_place_is_found_by_its_recorded_name(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere" / f"rollout-x-{THREAD}.jsonl"
    elsewhere.parent.mkdir()
    packed = elsewhere.with_name(elsewhere.name + ".zst")
    packed.write_bytes(b"")
    con = sqlite3.connect(tmp_path / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text)")
    con.execute("insert into threads values (?, ?)", (THREAD, str(elsewhere)))
    con.commit()
    con.close()
    assert find_rollout(tmp_path, THREAD) == packed  # not under sessions/: no walk found it


def test_a_rollback_ends_an_open_turn(tmp_path: Path):
    x = CodexRollout(THREAD)
    x.start("t1")
    x.user_says("hello", "u1")
    x.records.append(
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {"type": "thread_rolled_back", "num_turns": 1},
        }
    )
    path = write_rollout(tmp_path, THREAD, x.records)
    from duologue.codex.threads import last_turn_open

    assert not open_turn(read_lines(path)) and not last_turn_open(path)


def test_syncing_starts_after_a_rollback_that_follows_the_last_turn(tmp_path: Path):
    from duologue.engine import codex_start

    rolled = {"type": "thread_rolled_back", "num_turns": 1}
    path = write_rollout(
        tmp_path, THREAD, [*one_turn(), {"timestamp": "t", "type": "event_msg", "payload": rolled}]
    )
    cur = codex_start(path)
    assert cur.end == path.stat().st_size and cur.anchor == "turn-1"
