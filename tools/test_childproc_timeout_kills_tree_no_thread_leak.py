# -*- coding: utf-8 -*-
"""Regression test for the 22h thread leak traced to childproc.run(timeout=).

THE LEAK. subprocess.run(timeout=) kills only the DIRECT child on expiry, then calls
communicate() to join the stdout/stderr reader threads. If that child had spawned a
grandchild that inherited the pipe WRITE handles, those handles stay open, the readers
never reach EOF, communicate() blocks, and the reader threads park in Wait forever.
Over 22 hours a few per timed-out notify/chart/pip call accumulated to ~500 leaked OS
threads (524 vs ~33 fresh), none of them visible to py-spy because they carry no Python
frame -- they are the CPython pipe-reader threads stuck inside os.read.

WHAT THIS TEST PINS. childproc.run on the timeout path must drive Popen itself and, on
TimeoutExpired, kill the WHOLE tree (kill_tree -> taskkill /T on Windows, killpg on
POSIX) BEFORE the second communicate(), so the grandchild's write ends close, the
readers reach EOF, and every reader thread is joined. After a burst of timed-out calls
the count of non-main Python threads must return to its pre-burst baseline -- a leak of
even one reader thread per call is what this refuses to let back in.

WHY threading.enumerate() IS THE RIGHT GAUGE HERE. The leaked threads are CPython's own
_readerthread objects started by Popen.communicate(); they ARE Python Thread objects and
so they show up in threading.enumerate(). (They do not show in py-spy only because they
spend their life blocked in a C read with no Python frame on top.) So a return to
baseline here is exactly the condition whose violation produced the 500-thread figure.

ITERATIONS. Default 30 keeps CI honest and quick; set MCP_LEAK_REPRO_ITERS=200 to run
the full 200-iteration reproduction the investigation called for. Either way the
assertion is the same: zero net non-main threads after the burst.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import childproc  # noqa: E402


# A child that (a) outlives its own timeout and (b) spawns a grandchild which inherits the
# stdout/stderr pipes and also outlives the timeout. This is the exact shape that keeps the
# pipe write ends open past a direct-child kill, which is what leaked the reader threads.
_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "time.sleep(120)\n"
)


def _non_main_threads():
    return [t for t in threading.enumerate() if t is not threading.main_thread()]


def _iters() -> int:
    try:
        n = int(os.environ.get("MCP_LEAK_REPRO_ITERS", "30"))
    except ValueError:
        n = 30
    return max(1, n)


def test_timeout_path_kills_the_tree_and_leaks_no_reader_threads():
    import subprocess

    iters = _iters()
    # Let any transient startup threads settle, then record the baseline.
    time.sleep(0.5)
    base = len(_non_main_threads())

    for _ in range(iters):
        t0 = time.time()
        with pytest.raises(subprocess.TimeoutExpired):
            childproc.run([sys.executable, "-c", _CHILD], capture_output=True, timeout=1)
        # A direct-child-only kill would let communicate() block on the grandchild's
        # inherited pipe for the child's full lifetime (120s here); the tree kill returns in
        # ~1s. The ceiling is set well below 120s but high enough to ride out scheduler
        # spikes on a loaded host, so it still catches a regression to direct-kill while not
        # flaking when the box is busy.
        assert time.time() - t0 < 60, "timeout path did not return promptly -- tree not killed"

    # Reader threads, if leaked, are already parked; give the joiners a beat and measure.
    deadline = time.time() + 10
    while time.time() < deadline and len(_non_main_threads()) > base:
        time.sleep(0.2)
    leaked = _non_main_threads()
    assert len(leaked) == base, (
        "non-main thread count grew from %d to %d after %d timed-out calls -- reader "
        "threads leaked (names: %r)" % (base, len(leaked), iters, [t.name for t in leaked])
    )


def test_no_timeout_path_is_unchanged_and_decodes():
    # The fix touches only the timeout branch; the ordinary path must still decode UTF-8
    # output and return a real CompletedProcess.
    r = childproc.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('hi'); sys.stderr.write('er')"],
        capture_output=True,
    )
    assert r.returncode == 0 and r.stdout == "hi" and r.stderr == "er"


def test_timeout_path_still_decodes_and_honours_check_and_input():
    import subprocess

    r = childproc.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        input="ping", capture_output=True, timeout=10,
    )
    assert r.stdout.strip() == "ping"

    with pytest.raises(subprocess.CalledProcessError) as ei:
        childproc.run([sys.executable, "-c", "import sys; sys.exit(3)"],
                      capture_output=True, timeout=10, check=True)
    assert ei.value.returncode == 3


def test_timeout_path_tolerates_a_caller_supplied_spawn_kwarg():
    # On the timeout path the tree policy owns creationflags/start_new_session; a caller that
    # passes one must not cause a duplicate-**-key TypeError.
    kw = {"creationflags": 0} if os.name == "nt" else {"start_new_session": False}
    r = childproc.run([sys.executable, "-c", "print('ok')"],
                      capture_output=True, timeout=10, **kw)
    assert r.stdout.strip() == "ok"


def test_timed_call_preserves_subprocess_run_stub(monkeypatch):
    import subprocess

    calls = []
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = childproc.run(["fake-command"], timeout=3, input="hello")
    assert calls == [(["fake-command"], {"timeout": 3, "input": b"hello",
                                        "capture_output": True})]
    assert result.stdout == "ok"
