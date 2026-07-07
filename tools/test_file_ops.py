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
