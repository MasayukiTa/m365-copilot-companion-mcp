"""The resume loop's recover path must be BOUNDED and VISIBLE.

Root cause (2026-11-07): after a lost context the resume loop runs with sweep_active cleared,
so the stall watchdog -- which only arms while a sweep is live -- is blind to it. Every step it
takes there (cdp_alive, hard_reset, connect_over_cdp) is wrapped with an internal timeout that
covers the operation but NOT the OS work underneath (cdp_alive spawns a node driver and opens a
pipe). One such step blocked for ~33 hours inside 'attempt 1/3' and nothing wrote status.json or
reset anything.

The fix adds two guarantees, both tested here as pure units (no browser, no Playwright):
  1. _run_with_deadline caps any recover step on the wall clock and raises RecoveryTimeout when
     it is blown -- so a wedged spawn becomes a countable failure, not a silent freeze.
  2. _recovery_status produces a status.json payload stamped BETWEEN legs, so the file keeps
     moving and says 'recovering' (running=True) instead of looking dead.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from relay.fleet_runner import (  # noqa: E402
    RecoveryTimeout,
    _recovery_status,
    _run_with_deadline,
)


def test_deadline_returns_value_when_fn_is_fast():
    assert _run_with_deadline(lambda: 42, 5.0, label="fast") == 42


def test_deadline_reraises_the_fns_own_error():
    class Boom(Exception):
        pass

    def fn():
        raise Boom("from inside")

    with pytest.raises(Boom):
        _run_with_deadline(fn, 5.0, label="raises")


def test_deadline_raises_recoverytimeout_on_a_blocking_step():
    # The exact wedge: a step that never returns. It must be abandoned within the deadline,
    # not awaited. Daemon thread => it cannot hold the process open afterwards.
    started = time.time()
    with pytest.raises(RecoveryTimeout):
        _run_with_deadline(lambda: time.sleep(30), 0.3, label="wedge")
    # Came back on time (generous ceiling for a loaded CI box), not 30s later.
    assert time.time() - started < 5.0


def test_recovery_status_is_live_and_says_recovering():
    snap = _recovery_status(started=1000.0, attempt=1, max_recover=3, pending_n=17,
                            phase="cdp-probe", coordinator_started=1000.0, now=1234.5)
    # running stays True -- there is pending work -- so a reader never reads this as a finished
    # or dead run while it is merely between legs.
    assert snap["running"] is True
    assert snap["recovering"] is True
    assert snap["accepting_goals"] is True
    assert snap["recover_attempt"] == 1
    assert snap["recover_max"] == 3
    assert snap["recover_phase"] == "cdp-probe"
    assert snap["pending_goals"] == 17
    # updated advances with the clock so the file's mtime/updated moves each step.
    assert snap["updated"] == 1234.5
    assert snap["started"] == 1000.0


def test_recovery_status_updated_moves_between_calls():
    a = _recovery_status(1000.0, 1, 3, 5, "hard-reset", now=10.0)
    b = _recovery_status(1000.0, 1, 3, 5, "reconnect", now=11.0)
    assert b["updated"] > a["updated"]
    assert a["recover_phase"] == "hard-reset"
    assert b["recover_phase"] == "reconnect"
