# -*- coding: utf-8 -*-
"""A history the cockpit could not read must not be overwritten by what it archived instead.

MEASURED 2026-09-08. `.fleet/history.json` held 44 archived rows. The cockpit started at
18:21:10 with an empty in-memory history -- LoadHistory emptied the list first and swallowed
every failure, so a failed read was indistinguishable from a first run. Nothing reached a
terminal state for the next 24 minutes, so nothing saved and the file still held its 44 rows at
18:44. At 18:45 a run archived its first worker, SaveHistory fired, and the file became 1 row.
No message anywhere.

The scale is the part worth keeping: there are 1,878 transcripts across 218 runs on disk, and
the 44 rows that existed before this were themselves the remains of earlier rounds of the same
loss -- the oldest was from 03:40 that same morning. The self-improvement dashboard reads this
file, so "44 tasks, completion 0.318" was computed over the survivors, not over the work.

The trigger for that one failed load is NOT established and this does not claim to fix it. What
is fixed is the consequence: a failed load can no longer authorise the save that destroys the
evidence.
"""
import io
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent / "FleetCockpit.cs"


@pytest.fixture(scope="module")
def src():
    if not SRC.is_file():
        pytest.skip("FleetCockpit.cs not in this checkout")
    return io.open(SRC, encoding="utf-8", errors="replace").read()


def _body(src, name):
    """The text of one method, from its signature to the closing brace at its own indent."""
    m = re.search(r"^    void %s\(\)\s*\r?\n    \{" % re.escape(name), src, re.M)
    assert m, "%s not found" % name
    end = src.index("\n    }", m.end())
    return src[m.start():end]


def test_the_save_refuses_after_a_failed_load(src):
    """THE WHOLE FIX. _history holds only what this session archived; writing it over a file
    that was never read replaces every earlier row with this run's handful."""
    body = _body(src, "SaveHistory")
    assert "if (!_historyLoaded) return;" in body, \
        "SaveHistory will still overwrite a history it could not read"
    # and the guard must come BEFORE the write, not after it
    assert body.index("_historyLoaded") < body.index("WriteAllText")


def test_a_missing_file_counts_as_loaded(src):
    """An absent history is a first run, which is a successful load of nothing. Treating it as
    a failure would mean a fresh install could never write its first row."""
    body = _body(src, "LoadHistory")
    assert "if (!File.Exists(_historyPath)) { _historyLoaded = true; return; }" in body


def test_a_present_but_corrupt_file_does_not_count_as_loaded(src):
    """Present-but-unparseable is the dangerous case: there IS something to lose. The early
    `return` on a null cast must not pass through the success flag."""
    body = _body(src, "LoadHistory")
    i = body.index("if (arr == null) return;")
    j = body.index("_historyLoaded = true;", i)
    # the only success assignment after the null check is the one at the END of the try block,
    # after the rows have actually been read
    assert "foreach" in body[i:j], "the corrupt path reaches the success flag without reading rows"


def test_the_success_flag_is_set_only_after_the_rows_are_read(src):
    body = _body(src, "LoadHistory")
    assert body.index("foreach (object o in arr)") < body.index("_historyLoaded = true;\n        }")


def test_an_unreadable_file_is_kept_not_overwritten(src):
    """Refusing to save is not enough on its own -- the operator still has to be able to see
    what could not be read. Renaming also means the next save starts a clean file rather than
    appending to a damaged one."""
    assert "void PreserveUnreadableHistory()" in src
    body = _body(src, "PreserveUnreadableHistory")
    assert "File.Move(_historyPath, kept)" in body
    assert ".unreadable-" in body
    assert "File.Delete" not in body, "the unreadable file must be kept, not deleted"


def test_the_preserve_step_runs_on_the_failure_path_only(src):
    body = _body(src, "LoadHistory")
    assert "if (!_historyLoaded) PreserveUnreadableHistory();" in body
