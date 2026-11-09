# -*- coding: utf-8 -*-
"""A long-lived coordinator must pick up merged source, but only when it is safe.

The 2026-11-01 root cause: a coordinator started at 11:52 kept running pre-#388
relay_fleet.py for hours after the fix merged, so every job it composed ran stale code
(inject_probe_out/report.md A/C/D). The fix (relay/coordinator_reload.py) makes the
coordinator notice that a watched source is newer than its own start and exit cleanly --
with a DISTINCT exit code and its active marker left in place -- so the supervisor's
EXISTING fleet auto-resume relaunches a fresh coordinator on the new code. The four cases
below pin the contract the coordinator relies on:

  * newer source + idle  -> restart requested, with the distinct exit code
  * newer source + busy   -> no restart (a running worker is never killed)
  * unchanged source      -> nothing
  * the check itself errors -> nothing (fail safe)

Temp dirs only, a fake clock (the gate takes `now`), and fake mtimes (os.utime on real
files under tmp_path -- CodeWatch stats real files, so the files are real but disposable).
No network, no browser, no fleet. Linux-CI safe: pure os/time, no Windows-only calls.
"""
from __future__ import annotations

import os

from relay import coordinator_reload as C


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _make_sources(root):
    """Create the watched files under <root> with known content; return their abs paths.

    Only the files COORDINATOR_SOURCE_FILES names are created, each in its package dir, so
    CodeWatch fingerprints real files and a later os.utime is a real mtime change.
    """
    paths = {}
    for rel in C.COORDINATOR_SOURCE_FILES:
        ap = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        _write(ap, "# " + rel + "\nx = 1\n")
        paths[rel] = ap
    return paths


def _bump_mtime(path, when):
    """Rewrite a file and force its mtime to `when` (a fake, monotone-forward time)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("# changed\n")
    os.utime(path, (when, when))


# --------------------------------------------------------------------- pure decision

def test_decide_newer_and_idle_restarts():
    assert C.decide_coordinator_reload(source_stale=True, in_flight=False) == "restart"


def test_decide_newer_and_busy_waits():
    assert C.decide_coordinator_reload(source_stale=True, in_flight=True) == "wait"


def test_decide_stop_requested_never_restarts_even_when_idle():
    # An explicit operator stop owns the ending; a pending stop is never turned into a reload.
    assert C.decide_coordinator_reload(True, in_flight=False, stop_requested=True) == "wait"


def test_decide_unchanged_is_nothing():
    assert C.decide_coordinator_reload(source_stale=False, in_flight=False) == "none"
    assert C.decide_coordinator_reload(source_stale=False, in_flight=True) == "none"


# ----------------------------------------------------------------- gate, real files

def test_newer_source_and_idle_requests_restart_with_distinct_code(tmp_path):
    root = str(tmp_path)
    paths = _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)
    # t=1000: fresh, nothing changed -> none, whatever idle/busy.
    assert gate.should_restart(now=1000.0, in_flight=False) == "none"
    # a watched source is rewritten with a newer mtime than the process start.
    _bump_mtime(paths["relay/relay_fleet.py"], when=2000.0)
    # next check after the throttle interval, idle -> restart.
    assert gate.should_restart(now=1011.0, in_flight=False) == "restart"
    # the distinct exit code is the sysexits EX_TEMPFAIL, not any code main() already returns.
    assert C.COORDINATOR_RELOAD_EXIT_CODE == 75
    assert C.COORDINATOR_RELOAD_EXIT_CODE not in (0, 2, 3, 5, 130)


def test_newer_source_but_busy_does_not_restart(tmp_path):
    root = str(tmp_path)
    paths = _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)
    _bump_mtime(paths["relay/judge_gate.py"], when=2000.0)
    # work in flight -> wait, never restart; a running worker must not be killed.
    assert gate.should_restart(now=1011.0, in_flight=True) == "wait"
    # even on a later idle-but-still-busy... still wait while busy.
    assert gate.should_restart(now=1050.0, in_flight=True) == "wait"
    # and once it finally goes idle, it restarts.
    assert gate.should_restart(now=1100.0, in_flight=False) == "restart"


def test_unchanged_source_does_nothing(tmp_path):
    root = str(tmp_path)
    _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)
    # many ticks, no file ever changes -> always none.
    for t in (1000.0, 1011.0, 1022.0, 2000.0):
        assert gate.should_restart(now=t, in_flight=False) == "none"
        assert gate.should_restart(now=t + 0.5, in_flight=True) == "none"


def test_check_error_does_nothing(tmp_path):
    # A gate whose watch cannot be built (the CodeWatch ctor is forced to raise) fails safe:
    # should_restart returns "none", never "restart", even with a newer source conceptually.
    root = str(tmp_path)
    _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)

    class _Boom(object):
        def changed(self):
            raise RuntimeError("stat blew up")
        _stat = {}

    gate._watch = _Boom()
    assert gate.should_restart(now=1011.0, in_flight=False) == "none"
    # newest_source_mtime also swallows the error and returns 0.0.
    assert gate.newest_source_mtime() == 0.0


def test_throttle_limits_stat_passes(tmp_path):
    # Between interval boundaries the staleness is cached, so a change seen once stays seen and
    # a check inside the window does not re-stat. We prove the cache by flipping staleness only
    # across an interval boundary.
    root = str(tmp_path)
    paths = _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)
    assert gate.should_restart(now=1000.0, in_flight=False) == "none"      # first pass: clean
    _bump_mtime(paths["relay/task_kind.py"], when=2000.0)
    # within the same 10 s window: NOT re-stat'd, still reads clean (cached) -> none.
    assert gate.should_restart(now=1005.0, in_flight=False) == "none"
    # past the window: re-stat'd, now sees the change -> restart.
    assert gate.should_restart(now=1011.0, in_flight=False) == "restart"


def test_newest_source_mtime_is_exposed_for_status(tmp_path):
    root = str(tmp_path)
    paths = _make_sources(root)
    gate = C.CoordinatorReloadGate(root, interval=10.0, started=1000.0)
    _bump_mtime(paths["relay/relay_fleet.py"], when=2500.0)
    gate.should_restart(now=1011.0, in_flight=False)   # refresh the stat cache
    # the newest mtime among watched files is now at least the bumped value.
    assert gate.newest_source_mtime() >= 2500.0
