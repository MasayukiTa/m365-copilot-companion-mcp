# -*- coding: utf-8 -*-
"""The gateway must be able to say what it is running now, over a path the jam cannot block.

WHY THIS EXISTS. 2026-10-27..28 the tool gateway jammed three times. Each time tools/list still
answered -- so the server looked alive -- but no tools/call started, and the only fact anyone
had was "tools/list works, tools/call does not". That does not name the occupant or its age, so
the only recovery was a restart, which also destroyed the evidence (issue #328, proposal 2).

The fix is tools/exec_inflight: a process-local, thread-safe inventory the call_tool gateway
fills on entry and empties in its finally, read by main.py's /exec_status route over a path
separate from tools/list and from the tool path itself. These tests pin the properties that
matter when the gateway is actually stuck:

  1. A call that has entered and NOT left is visible (that is the jam -- a tool that never
     returned), and its elapsed time grows; end() removes exactly it.
  2. end() is idempotent and total, so the gateway's unconditional finally is safe even when
     begin() never ran.
  3. The snapshot puts the LONGEST-running call first -- the occupant an operator is hunting.
  4. Two simultaneous calls to the same tool are two distinct rows, so one ending does not
     erase the other.
  5. Concurrency: begin/end from many threads never corrupts the inventory or miscounts.

BEFORE the fix (no exec_inflight module, no begin/end in the gateway, no /exec_status route)
there was nothing to import and nothing to read, so every assertion here fails; after it, they
pass. These do NOT import main (it pulls in fastmcp, unavailable to a plain unit run); the
visibility logic lives entirely in tools.exec_inflight, and the route is a thin reader of
snapshot() whose auth branch is exercised by reasoning stated in main.py's docstring.
"""
from __future__ import annotations

import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from tools import exec_inflight as EI  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    EI._reset_for_test()
    yield
    EI._reset_for_test()


def test_a_call_that_has_not_ended_is_visible():
    """The jam is a call that entered and did not leave. It must show in the snapshot."""
    assert EI.executing_count() == 0
    tok = EI.begin("find_files", call_id="c1")
    snap = EI.snapshot()
    assert snap["executing_count"] == 1
    assert len(snap["executing"]) == 1
    row = snap["executing"][0]
    assert row["tool"] == "find_files"
    assert row["call_id"] == "c1"
    assert row["elapsed_s"] >= 0.0
    EI.end(tok)
    assert EI.executing_count() == 0
    assert EI.snapshot()["executing"] == []


def test_elapsed_grows_while_the_call_is_held():
    """A held call's elapsed time increases -- that is how an operator sees it is STUCK, not
    merely present."""
    tok = EI.begin("glob", started_mono=time.monotonic() - 5.0)
    elapsed = EI.snapshot()["executing"][0]["elapsed_s"]
    assert elapsed >= 5.0, "a call begun 5s ago should read at least 5s elapsed, got %r" % elapsed
    EI.end(tok)


def test_end_is_idempotent_and_total():
    """The gateway calls end() unconditionally in a finally. A missing or repeated token is a
    no-op, never an error."""
    EI.end(None)            # begin() never ran
    EI.end(999999)          # a token that was never issued
    tok = EI.begin("grep")
    EI.end(tok)
    EI.end(tok)             # ending twice must not raise or go negative
    assert EI.executing_count() == 0


def test_the_longest_running_call_is_reported_first():
    """The occupant an operator hunts is the one holding the floor longest, so it sorts first."""
    now = time.monotonic()
    EI.begin("read_file", started_mono=now - 1.0)
    EI.begin("find_files", started_mono=now - 30.0)   # the occupant
    EI.begin("glob", started_mono=now - 10.0)
    rows = EI.snapshot()["executing"]
    assert [r["tool"] for r in rows] == ["find_files", "glob", "read_file"]
    assert rows[0]["elapsed_s"] >= 30.0
    assert EI.snapshot()["longest_elapsed_s"] >= 30.0


def test_two_calls_to_the_same_tool_are_two_rows():
    """Keyed on a per-call token, not the tool name: ending one does not erase the other."""
    a = EI.begin("find_files")
    b = EI.begin("find_files")
    assert a != b
    assert EI.executing_count() == 2
    EI.end(a)
    assert EI.executing_count() == 1
    assert EI.snapshot()["executing"][0]["tool"] == "find_files"
    EI.end(b)
    assert EI.executing_count() == 0


def test_concurrent_begin_end_never_corrupts_the_inventory():
    """Many threads entering and leaving the execute path at once must leave a consistent count.
    The gateway runs tool bodies in anyio worker threads, so begin/end are genuinely concurrent.
    """
    errors = []

    def worker(n):
        try:
            for _ in range(200):
                t = EI.begin("tool%d" % (n % 5))
                EI.end(t)
        except Exception as e:  # pragma: no cover - a failure here is the finding
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    # Every begin was paired with an end, so nothing is left in flight.
    assert EI.executing_count() == 0
    assert EI.snapshot()["executing_count"] == 0


def test_snapshot_is_plain_json_builtins():
    """The /exec_status route returns snapshot() straight to JSONResponse, so every value must be
    a built-in with no custom objects that json cannot encode."""
    import json
    EI.begin("find_files", call_id="abc")
    text = json.dumps(EI.snapshot())   # raises if a value is not JSON-serialisable
    assert "find_files" in text
    assert "executing_count" in text
