# -*- coding: utf-8 -*-
"""Is the process running the code that is on disk?

THE INCIDENT. Every shadow assessment in a benchmark run came back UNVERIFIABLE, reason "no tool
calls recorded for this task". The evidence ledger held zero rows and its file did not exist.
The gateway wiring was correct, and a test asserted it was correct -- by reading main.py's
source. The server had started at 17:01; the wiring was written at 19:20. The code was right and
had never run.

NO TEST THAT READS A FILE CAN FIND THIS, because every test imports from disk and therefore sees
the new code. The defect lives in the gap between the file and the process. Worse, the symptom
read as a different problem entirely: UNVERIFIABLE was literally true, and it looked like the
workers had done nothing rather than like nothing was recording.
"""
import os
import time

import pytest

from tools import deploy_freshness as F


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "relay").mkdir()
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tools" / "a.py").write_text("x = 1\n", encoding="utf-8")
    old = time.time() - 7200
    for p in (tmp_path / "main.py", tmp_path / "tools" / "a.py"):
        os.utime(str(p), (old, old))
    return tmp_path


def touch(path, when=None):
    when = when or time.time()
    os.utime(str(path), (when, when))


# -- the three answers -------------------------------------------------------------------

def test_a_file_changed_after_the_process_started_is_reported(tree):
    started = time.time() - 3600
    touch(tree / "tools" / "a.py")
    stale = F.newer_than(started, root=str(tree))
    assert [f for f, _ in stale] == [os.path.join("tools", "a.py")]


def test_the_gateway_itself_counts_even_though_it_is_not_in_a_watched_directory(tree):
    """main.py is where the dispatch and the ledger wiring live -- the exact file that was
    stale in the incident. Watching only package directories would have missed it."""
    touch(tree / "main.py")
    assert "main.py" in [f for f, _ in F.newer_than(time.time() - 3600, root=str(tree))]


def test_nothing_newer_means_fresh(tree):
    assert F.newer_than(time.time(), root=str(tree)) == []


def test_a_missing_process_is_unknown_not_fresh(monkeypatch):
    """FAIL CLOSED. A freshness check that says fresh when it does not know is the thing it
    exists to prevent."""
    monkeypatch.setattr(F, "server_processes", lambda pattern="main.py": [])
    r = F.check()
    assert r["fresh"] is None and "no main.py process" in r["why"]


def test_no_psutil_is_unknown_not_fresh(monkeypatch):
    monkeypatch.setattr(F, "server_processes", lambda pattern="main.py": None)
    assert F.check()["fresh"] is None


def test_an_unreadable_start_time_is_unknown_not_fresh(monkeypatch):
    monkeypatch.setattr(F, "server_processes", lambda pattern="main.py": [(1, 0.0)])
    r = F.check()
    assert r["fresh"] is None and "start time" in r["why"]


def test_a_stale_process_is_reported_with_the_files_and_the_age(tree, monkeypatch):
    started = time.time() - 5 * 3600
    monkeypatch.setattr(F, "server_processes", lambda pattern="main.py": [(1, started)])
    touch(tree / "main.py")
    r = F.check(root=str(tree))
    assert r["fresh"] is False
    assert "main.py" in r["stale_files"]
    assert "5.0 h ago" in r["why"]


# -- what it must not cry wolf about -------------------------------------------------------

def test_a_test_file_changing_is_not_staleness(tree):
    """A check that fires on things that cannot change the server's behaviour gets ignored,
    and an ignored check is worse than none."""
    (tree / "tools" / "test_a.py").write_text("x = 2\n", encoding="utf-8")
    (tree / "tools" / "conftest.py").write_text("x = 2\n", encoding="utf-8")
    assert F.newer_than(time.time() - 3600, root=str(tree)) == []


def test_compiled_caches_are_not_staleness(tree):
    cache = tree / "tools" / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-310.pyc").write_bytes(b"\x00")
    assert F.newer_than(time.time() - 3600, root=str(tree)) == []


def test_a_data_file_is_not_staleness(tree):
    (tree / "tools" / "notes.md").write_text("hello", encoding="utf-8")
    (tree / "tools" / "data.json").write_text("{}", encoding="utf-8")
    assert F.newer_than(time.time() - 3600, root=str(tree)) == []


# -- against the real repository -----------------------------------------------------------

def test_it_answers_about_this_machine_without_raising():
    """The three answers are all acceptable here -- the server may not be running during a test
    run. What must not happen is an exception, because this is meant to be callable from a
    monitor that is already dealing with something going wrong."""
    r = F.check()
    assert r["fresh"] in (True, False, None)
    assert isinstance(r["stale_files"], list)
