# -*- coding: utf-8 -*-
"""A file-search walk must be bounded, and the bound must be reachable without a restart.

WHY THIS EXISTS. 2026-10-27..28 the tool gateway jammed three times, each time on a single
find_files / glob that walked an unbounded tree; the only recovery was restarting the server
(issue #328). The walk in tools/walk.py already carried a wall-clock deadline and a pruned-dir
list, but the two numbers that bound it -- how long one walk may run and how deep it descends
-- were hard-coded constants with no depth cap at all. This file pins the three properties the
fix adds:

  1. iter_files(..., max_depth=N) descends exactly N levels from the search root and no
     further, pruning the deeper directories WHILE walking (so the cost is never paid), and
     max_depth=None keeps the pre-#328 behaviour of no limit.
  2. search_max_depth() and search_timeout_seconds() follow the settings file: a value written
     after the module is imported is seen on the NEXT call, with nothing restarted -- the same
     promise tools/settings_keys.py declares for these keys.
  3. The defaults, when the settings file says nothing, are the pre-#328 constants (no depth
     limit, 20 s, 4 concurrent), so no existing legitimate search changes until an owner asks.

These tests deliberately do NOT import tools.search_ops: that module pulls in fastmcp, which a
plain unit run need not have, and the depth/time logic they check lives entirely in
tools.walk. The find_files / glob wiring that passes max_depth through is a one-line argument
hand-off covered by reading the signature; the behaviour that could be wrong is here.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import settings_path as SP   # noqa: E402
from tools import walk as W             # noqa: E402


# --------------------------------------------------------------------- a tree of known depth
def _make_tree(root):
    """Create root/f0.txt, root/a/f1.txt, root/a/b/f2.txt, root/a/b/c/f3.txt.

    So a file named fN sits at depth N counted from root (f0 directly under root is depth 0).
    """
    here = root
    for level, child in enumerate((None, "a", "b", "c")):
        if child is not None:
            here = os.path.join(here, child)
            os.makedirs(here, exist_ok=True)
        io.open(os.path.join(here, "f%d.txt" % level), "w", encoding="utf-8").write("x")


def _depths_found(base, max_depth):
    """The set of file depths iter_files returns, read back from the fN filenames."""
    found = set()
    for p in W.iter_files(base, max_depth=max_depth):
        name = os.path.basename(str(p))
        assert name.startswith("f") and name.endswith(".txt"), name
        found.add(int(name[1:-4]))
    return found


@pytest.mark.parametrize("max_depth, expected", [
    (0, {0}),              # only the root directory's own files
    (1, {0, 1}),           # root + one level
    (2, {0, 1, 2}),
    (3, {0, 1, 2, 3}),
    (99, {0, 1, 2, 3}),    # a cap deeper than the tree returns everything, never raises
])
def test_max_depth_descends_exactly_that_many_levels(tmp_path, max_depth, expected):
    base = tmp_path / "root"
    base.mkdir()
    _make_tree(str(base))
    assert _depths_found(Path(str(base)), max_depth) == expected, \
        "max_depth=%d should return files at depths %r" % (max_depth, sorted(expected))


def test_max_depth_none_has_no_limit(tmp_path):
    """None is the pre-#328 behaviour: every file, however deep."""
    base = tmp_path / "root"
    base.mkdir()
    _make_tree(str(base))
    assert _depths_found(Path(str(base)), None) == {0, 1, 2, 3}


def test_a_too_deep_directory_is_not_descended_into(tmp_path, monkeypatch):
    """The saving is in NOT walking the deep tree, not in filtering its files afterwards. Prove
    the descent does not happen by spying on os.walk and asserting the too-deep directory is
    never visited."""
    base = tmp_path / "root"
    base.mkdir()
    _make_tree(str(base))

    visited = []
    real_walk = os.walk

    def spy_walk(top, *a, **k):
        for row in real_walk(top, *a, **k):
            visited.append(row[0])
            yield row

    monkeypatch.setattr(W.os, "walk", spy_walk)
    list(W.iter_files(Path(str(base)), max_depth=1))
    deepest = os.path.join(str(base), "a", "b", "c")
    assert deepest not in visited, \
        "max_depth=1 descended into %r; the deep tree's cost was paid anyway" % deepest


# ------------------------------------------------------------- the bounds follow the settings
def _point_settings_at(tmp_path, monkeypatch, **pairs):
    path = tmp_path / "settings.txt"
    with io.open(str(path), "w", encoding="utf-8", newline="") as fh:
        for k, v in pairs.items():
            fh.write("%s=%s\n" % (k, v))
    monkeypatch.setattr(SP, "NEW_PATH", str(path))
    return path


def test_search_max_depth_follows_the_file_without_a_restart(tmp_path, monkeypatch):
    path = _point_settings_at(tmp_path, monkeypatch, search_max_depth="2")
    assert W.search_max_depth() == 2
    with io.open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write("search_max_depth=0\n")
    assert W.search_max_depth() == 0


def test_search_timeout_seconds_follows_the_file_without_a_restart(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, search_timeout_seconds="45")
    assert W.search_timeout_seconds() == 45
    _point_settings_at(tmp_path, monkeypatch, search_timeout_seconds="7")
    assert W.search_timeout_seconds() == 7


def test_absent_settings_give_the_pre_328_defaults(tmp_path, monkeypatch):
    """An empty settings file means: no depth limit, the old 20 s budget, 4 concurrent."""
    _point_settings_at(tmp_path, monkeypatch)   # writes an empty file
    assert W.search_max_depth() is None
    assert W.search_timeout_seconds() == 20
    assert W.search_concurrency() == 4


@pytest.mark.parametrize("bad", ["", "-3", "not-a-number", "0"])
def test_a_malformed_or_nonpositive_timeout_falls_back_not_crashes(tmp_path, monkeypatch, bad):
    """A budget of 0, a negative, or junk must not produce a zero/negative deadline -- that
    would make every search time out instantly. It falls back to the default instead."""
    _point_settings_at(tmp_path, monkeypatch, search_timeout_seconds=bad)
    assert W.search_timeout_seconds() == 20


def test_a_negative_or_junk_depth_means_no_limit(tmp_path, monkeypatch):
    """search_max_depth is a cap; a negative or unparseable value is not a tighter cap, it is
    no cap -- the safe reading, since the default is already no limit."""
    for bad in ("-1", "not-a-number"):
        _point_settings_at(tmp_path, monkeypatch, search_max_depth=bad)
        assert W.search_max_depth() is None, bad


def test_the_deadline_uses_the_settings_value(tmp_path, monkeypatch):
    """WalkDeadline with no explicit seconds adopts the settings budget, so raising the setting
    lengthens the next walk's deadline with no restart; an explicit per-call seconds still
    wins, and a non-positive explicit value is ignored in favour of the setting."""
    _point_settings_at(tmp_path, monkeypatch, search_timeout_seconds="33")
    assert W.WalkDeadline().seconds == 33
    assert W.WalkDeadline(seconds=5).seconds == 5
    assert W.WalkDeadline(seconds=0).seconds == 33
