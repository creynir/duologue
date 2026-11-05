import json
import stat

from duologue.translate.outputs import OutputStore


def test_short_text_passes_through_and_writes_nothing(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=10)

    assert store.keep("ref", "short") == "short"
    assert not (tmp_path / "out").exists()


def test_long_text_is_cut_after_a_pointer_to_the_full_file(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=10)
    text = "0123456789abcdef"

    kept = store.keep("toolu_1", text)

    path = tmp_path / "out" / "toolu_1.txt"
    assert kept == f"[16 characters; the full output is in {path}; the start of it:]\n0123456789"
    assert path.read_text() == text


def test_an_explicit_limit_overrides_the_model_limit(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=4, ui_limit=8)

    assert store.keep("r", "0123456789", store.ui_limit).endswith("\n01234567")


def test_non_text_is_kept_as_json(tmp_path):
    store = OutputStore(tmp_path / "out")
    value = [{"type": "text", "text": "x"}]

    assert store.keep("r", value) == json.dumps(value)


def test_unsafe_and_long_refs_make_safe_short_file_names(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=1)
    store.keep("../../etc/passwd", "xx")
    store.keep("a" * 200, "xx")
    store.keep("a" * 199 + "b", "xx")

    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert all("/" not in n and len(n) <= 81 for n in names)
    assert len(names) == 3  # two long refs that share a prefix still get their own files


def test_refs_that_sanitise_alike_keep_their_own_files(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=1)
    first = store.keep("pair:turn:a/b", "first")
    second = store.keep("pair_turn_a_b", "second")

    assert first != second
    files = {p.read_text() for p in (tmp_path / "out").iterdir()}
    assert files == {"first", "second"}


def test_full_outputs_are_private_to_the_user(tmp_path):
    store = OutputStore(tmp_path / "out", model_limit=1)
    store.keep("r", "xx")

    mode = stat.S_IMODE((tmp_path / "out" / "r.txt").stat().st_mode)
    assert mode == 0o600


def test_an_existing_file_is_kept_only_when_it_is_ours(tmp_path):
    import os

    store = OutputStore(tmp_path / "out", model_limit=5)
    store.keep("ref", "x" * 50)
    [kept] = list((tmp_path / "out").iterdir())
    before = kept.stat().st_mtime_ns
    store.keep("ref", "x" * 50)  # the same output again: left as it is
    assert kept.stat().st_mtime_ns == before
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x" * 50)
    kept.unlink()
    os.symlink(elsewhere, kept)  # a link planted where the output goes is replaced, not used
    store.keep("ref", "x" * 50)
    assert not kept.is_symlink() and oct(kept.stat().st_mode & 0o777) == "0o600"
