"""Hermetic tests for tools/file_ops.py -- the relative-path anchoring fix.

Background (do not re-investigate, see main.py / file_ops.py comments): a
relative path like "Desktop" used to be resolved against the MCP server
process's CWD (the repo root) via plain Path(path).expanduser().resolve().
The repo root happens to contain a decoy "Desktop/" folder (untracked demo
data), so an agent asking for "Desktop" silently got the decoy instead of
the real user profile Desktop, and then wrongly reported the real files as
missing / "outside the allowed base".

These tests are fully hermetic: they fake HOME via the USERPROFILE env var
and fake CWD via monkeypatch.chdir into tmp_path fixtures, so nothing here
touches a real user's Desktop/Documents/etc.

Run: pytest -q tools\\test_file_ops.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import file_ops


# ===========================================================================
# 1. _validate_path anchoring
# ===========================================================================


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "fake_repo_cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def test_known_user_folder_forward_slash_anchors_to_home(fake_home, fake_cwd):
    p = file_ops._validate_path("Desktop")
    assert p == (fake_home / "Desktop").resolve()
    assert p != (fake_cwd / "Desktop").resolve()


def test_known_user_folder_subpath_anchors_to_home(fake_home, fake_cwd):
    p = file_ops._validate_path("Desktop/sub")
    assert p == (fake_home / "Desktop" / "sub").resolve()


def test_known_user_folder_backslash_subpath_anchors_to_home(fake_home, fake_cwd):
    p = file_ops._validate_path("Desktop\\x")
    assert p == (fake_home / "Desktop" / "x").resolve()


def test_known_user_folder_is_case_insensitive(fake_home, fake_cwd):
    p = file_ops._validate_path("desktop")
    assert p == (fake_home / "desktop").resolve()
    p2 = file_ops._validate_path("DOCUMENTS")
    assert p2 == (fake_home / "DOCUMENTS").resolve()


def test_non_user_folder_relative_path_anchors_to_cwd(fake_home, fake_cwd):
    p = file_ops._validate_path("main.py")
    assert p == (fake_cwd / "main.py").resolve()
    assert p != (fake_home / "main.py").resolve()


def test_repo_relative_subpath_anchors_to_cwd(fake_home, fake_cwd):
    p = file_ops._validate_path("tools/x.py")
    assert p == (fake_cwd / "tools" / "x.py").resolve()


def test_absolute_path_is_unchanged(fake_home, fake_cwd, tmp_path):
    target = tmp_path / "somewhere" / "else.txt"
    p = file_ops._validate_path(str(target))
    assert p == target.resolve()


def test_tilde_path_still_expands_to_home_directly(fake_home, fake_cwd):
    # A path the caller already prefixed with "~" must keep using expanduser's
    # normal behavior (home dir), not the known-folder special case.
    p = file_ops._validate_path("~/Desktop")
    assert p == (fake_home / "Desktop").resolve()


# ===========================================================================
# 2. Not-found messaging: points to find_files, never says "allowed base"
# ===========================================================================


def test_list_directory_not_found_message(tmp_path):
    missing = tmp_path / "does_not_exist_dir"
    msg = file_ops.list_directory(path=str(missing))
    assert "not found" in msg
    assert str(missing.resolve()) in msg or str(missing) in msg
    assert "find_files" in msg
    assert "allowed base" not in msg


def test_read_file_not_found_message(tmp_path):
    missing = tmp_path / "does_not_exist.txt"
    msg = file_ops.read_file(path=str(missing))
    assert "not found" in msg
    assert "find_files" in msg
    assert "allowed base" not in msg


def test_file_metadata_not_found_message(tmp_path):
    missing = tmp_path / "does_not_exist_meta.txt"
    msg = file_ops.file_metadata(path=str(missing))
    assert "not found" in msg
    assert "find_files" in msg
    assert "allowed base" not in msg


def test_list_directory_still_works_for_existing_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    msg = file_ops.list_directory(path=str(tmp_path))
    assert "a.txt" in msg
    assert "not found" not in msg


# ===========================================================================
# 3. OPS-17: read_file streams instead of loading the whole file
# ===========================================================================
#
# Old implementation: `p.read_text(encoding=encoding).splitlines()`, sliced AFTER
# decoding the whole file. max_lines=1 against a multi-GB file still decoded and
# held every line in memory before throwing all but one away. The fix streams the
# file line-by-line and stops as soon as the requested range (or the hard byte cap)
# is satisfied.


def test_read_file_normal_small_file_output_unchanged(tmp_path):
    """Format/behaviour for an ordinary small file must be byte-identical to before:
    "N: line" per line, 1-based, joined with \\n."""
    f = tmp_path / "small.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    assert file_ops.read_file(path=str(f)) == "1: alpha\n2: beta\n3: gamma\n4: delta"


def test_read_file_start_line_and_max_lines_unchanged(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    assert file_ops.read_file(path=str(f), start_line=2, max_lines=2) == "2: beta\n3: gamma"


def test_read_file_start_line_past_end_returns_empty(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    assert file_ops.read_file(path=str(f), start_line=99) == ""


def test_read_file_max_lines_one_returns_only_first_requested_line(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    assert file_ops.read_file(path=str(f), max_lines=1) == "1: alpha"
    assert file_ops.read_file(path=str(f), start_line=2, max_lines=1) == "2: beta"


def test_read_file_no_trailing_newline_last_line_included(tmp_path):
    f = tmp_path / "small.txt"
    f.write_bytes(b"one\ntwo\nthree")  # no trailing newline
    assert file_ops.read_file(path=str(f)) == "1: one\n2: two\n3: three"


def test_read_file_byte_cap_truncates_and_says_so(tmp_path, monkeypatch):
    """With the cap lowered to something small, an unbounded read (max_lines=None)
    of a file that exceeds it stops early and appends a clear truncation note
    naming start_line/max_lines as the way to read the rest -- rather than silently
    returning a partial file with no indication anything was cut."""
    monkeypatch.setattr(file_ops, "READ_FILE_MAX_OUTPUT_BYTES", 20)
    f = tmp_path / "medium.txt"
    f.write_text("\n".join(f"line{i:03d}" for i in range(100)), encoding="utf-8")
    out = file_ops.read_file(path=str(f))
    assert "[read_file: truncated at 20 bytes" in out
    assert "start_line/max_lines" in out
    # And it must not have silently returned everything.
    assert "line099" not in out


def test_read_file_byte_cap_not_hit_for_small_file(tmp_path):
    """The default cap (2 MB) must never fire for an ordinary small file."""
    f = tmp_path / "small.txt"
    f.write_text("just a few lines\nof ordinary text\n", encoding="utf-8")
    out = file_ops.read_file(path=str(f))
    assert "truncated" not in out


def _write_big_text_file(path: Path, target_bytes: int) -> None:
    """Fast bulk-write of a large but simple text file: fixed-width lines repeated
    until target_bytes is exceeded, so generating e.g. 200 MB takes a fraction of a
    second rather than looping in Python line by line."""
    chunk = (("x" * 200) + "\n").encode("utf-8") * 5000  # ~1 MB per chunk
    with path.open("wb") as fh:
        written = 0
        while written < target_bytes:
            fh.write(chunk)
            written += len(chunk)
        fh.write(b"LAST\n")


def test_read_file_max_lines_one_peak_memory_bounded_on_large_file(tmp_path):
    """OPS-17 regression, run in an isolated subprocess so the measurement is the
    memory read_file(max_lines=1) itself adds -- not conflated with whatever
    importing tools.file_ops (which pulls in fastmcp transitively via
    tools.security) already costs before the call even happens.

    Measures the DELTA between peak working-set sampled right after imports
    (baseline: everything read_file needed loaded, nothing it will read yet)
    and peak working-set sampled after the call returns. peak_wset is a
    monotonic high-water mark, so this delta is exactly "how much additional
    peak memory did this one read_file call cause" regardless of how much the
    interpreter/import graph already used. The old
    `.read_text(...).splitlines()` implementation would add roughly the whole
    decoded file to that watermark; streaming should add only a small,
    roughly constant amount regardless of file size.
    """
    target_bytes = 200 * 1024 * 1024  # 200 MB
    big = tmp_path / "big.txt"
    _write_big_text_file(big, target_bytes)
    file_size = big.stat().st_size
    assert file_size >= target_bytes

    repo_root = Path(__file__).resolve().parent.parent
    probe = tmp_path / "_probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "import psutil\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from tools import file_ops\n"
        "proc = psutil.Process(os.getpid())\n"
        "def _peak():\n"
        "    mi = proc.memory_info()\n"
        "    return getattr(mi, 'peak_wset', None) or mi.rss\n"
        "baseline_peak = _peak()\n"
        f"out = file_ops.read_file(path={str(big)!r}, max_lines=1)\n"
        "after_peak = _peak()\n"
        "sys.stdout.write(json.dumps({\n"
        "    'baseline_peak': baseline_peak, 'after_peak': after_peak, 'out': out,\n"
        "}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"probe subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["out"] == "1: " + "x" * 200
    delta = data["after_peak"] - data["baseline_peak"]
    # Generous absolute floor (interpreter/IO buffering noise) AND a fraction of
    # file size (so this still fails hard if streaming regresses to a full load).
    budget = max(20 * 1024 * 1024, int(file_size * 0.1))
    assert delta < budget, (
        f"read_file(max_lines=1) added {delta:,} bytes to peak working set "
        f"(budget {budget:,}) reading a {file_size:,}-byte file -- looks like "
        "the whole file was loaded instead of streamed"
    )
