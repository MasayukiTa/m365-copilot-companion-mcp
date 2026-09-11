# -*- coding: utf-8 -*-
"""Opening a tab is not a wedged browser.

THE INCIDENT, read out of a real run (.fleet/coordinator_20260911_082928_p22152.log). An n=100
SWE-bench A/B captured NOTHING in 102 minutes. The coordinator's own events, in order:

    [relay_fleet] w12: socket -> tab (ChatHubError: frame budget exhausted before completion)
    [watchdog] fleet stalled 150s -> hard-resetting the Edge (no eval in flight -> wedged)
    [recover] Edge context lost; resuming 16 goal(s) (attempt 1/3)
    [relay_fleet] w6: socket -> tab (ChatHubError: frame budget exhausted before completion)
    [watchdog] fleet stalled 150s -> hard-resetting the Edge (no eval in flight -> wedged)
    [recover] Edge context lost; resuming 11 goal(s) (attempt 2/3)
    [watchdog] fleet stalled 150s -> hard-resetting the Edge  (x2)
    [recover] Edge context lost; resuming 4 goal(s) (attempt 3/3)

Every stall is immediately preceded by a socket->tab fallback, and every reset costs every
unfinished goal its progress: FleetContextLost resumes them at turn 1.

THE MECHANISM, from the code rather than the pattern. The fallback calls _open_fresh, which is
documented to allow THREE navigation attempts of 45s plus a 25s composer wait each -- 210s --
and "up to ~300s" once a sign-in page has been surfaced. That is a synchronous call on the
single-threaded round-robin, so status.json cannot be updated while it runs. The watchdog's rule
(fleet_runner._watchdog_should_reset) resets a browser that has not advanced for stall_s UNLESS
some worker declares a bounded eval -- and this path, alone among the long blocking ones, never
declared one. So a healthy Edge, busy doing exactly what it was asked to do, reads as wedged.

WHY THE FIX IS NOT "RAISE THE THRESHOLD". 150s is the right question to ask of a browser that is
doing nothing. The defect is that the fleet was doing something and did not say so, which is
precisely what eval_busy_until exists to express -- _mark_eval_busy's own comment says it flushes
a snapshot "so the watchdog sees the marker before the sweep freezes". A bigger number would
merely move the livelock and would blind the watchdog to real wedges for longer.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fleet_runner as FR      # noqa: E402
from relay import relay_fleet as RF       # noqa: E402


def _status(workers, running=True):
    return {"running": running, "idle": False, "workers": workers}


#: What the snapshot looked like during the fallback: the worker is back to "ready" (the
#: fallback sets that before re-sending), and nothing declares a window.
UNDECLARED = [{"name": "w12", "status": "ready", "eval_busy_until": 0.0}]


# ── the reproduction ──────────────────────────────────────────────────────────────────────

def test_a_tab_open_with_no_declared_window_reads_as_a_wedge():
    """THE DEFECT ITSELF, at the decision that made it. 210s is _open_fresh's ordinary bound
    (3 x (45s goto + 25s composer)); the browser is fine and is hard-reset anyway."""
    should, why = FR._watchdog_should_reset(_status(UNDECLARED), stalled_s=210)
    assert should is True
    assert "no eval in flight" in why


def test_the_fallback_bound_really_does_exceed_the_stall_threshold():
    """Not a guess: the numbers come from _open_fresh's own comments, and the reset fires at
    150s. If these ever stopped overlapping, the reproduction above would be theatre."""
    assert RF.FALLBACK_OPEN_CEILING_S >= 210, (
        "the declared window is shorter than the operation it covers")
    # The watchdog's own default stall threshold is smaller than the operation's bound, which
    # is the whole overlap the incident lived in.
    assert 150 < RF.FALLBACK_OPEN_CEILING_S


# ── the fix ───────────────────────────────────────────────────────────────────────────────

def test_a_declared_window_is_not_a_wedge():
    declared = [{"name": "w12", "status": "ready",
                 "eval_busy_until": time.time() + RF.FALLBACK_OPEN_CEILING_S}]
    should, why = FR._watchdog_should_reset(_status(declared), stalled_s=210)
    assert should is False
    assert "within eval deadline" in why


def test_the_window_is_bounded_so_a_real_wedge_is_still_recovered():
    """A declaration must not be a blank cheque. Past its own deadline the worker stops being
    an excuse, and the browser is reset -- which is the failsafe the watchdog was built around."""
    expired = [{"name": "w12", "status": "ready", "eval_busy_until": time.time() - 1}]
    should, _why = FR._watchdog_should_reset(_status(expired),
                                             stalled_s=FR.EVAL_STALL_CEILING_S + 10)
    assert should is True


def test_declaring_the_window_does_not_claim_the_worker_is_verifying():
    """_mark_eval_busy also flips the card to 'verifying', which would be a lie here -- this
    worker is opening a tab, not running an acceptance check. The watchdog reads
    eval_busy_until BEFORE it looks at status, so the honest marker is enough on its own."""
    w = RF.RelayWorker("investigate the thing", "w0")
    w.status = "ready"
    w._declare_blocking(RF.FALLBACK_OPEN_CEILING_S)
    try:
        assert w.eval_busy_until > time.time()
        assert w.status == "ready", "the card was made to claim a verification that is not running"
    finally:
        w._end_blocking()
    assert w.eval_busy_until == 0.0


def test_the_window_is_flushed_so_the_watchdog_can_see_it_before_the_freeze():
    """Setting the field is not enough: the watchdog reads status.json, and the sweep is about
    to stop writing it. _mark_eval_busy's own comment says the flush is the point."""
    w = RF.RelayWorker("investigate the thing", "w0")
    flushes = []
    w._busy_writer = lambda: flushes.append(time.time())
    w._declare_blocking(RF.FALLBACK_OPEN_CEILING_S)
    assert flushes, "no snapshot was flushed, so the marker never reached the watchdog"
    w._end_blocking()
    assert len(flushes) == 2, (
        "the window was not flushed on close, so the snapshot keeps claiming a blocking call "
        "that already finished -- which blinds the watchdog to a genuine wedge")


def test_closing_the_window_can_never_fail_the_worker():
    w = RF.RelayWorker("investigate the thing", "w0")

    def _boom():
        raise RuntimeError("snapshot writer exploded")
    w._busy_writer = _boom
    w._declare_blocking(RF.FALLBACK_OPEN_CEILING_S)   # must not raise
    w._end_blocking()                                  # must not raise
    assert w.eval_busy_until == 0.0


def test_the_fallback_declares_the_window_around_the_tab_open():
    """The sweep, not just the helper: the one long blocking call in the fallback has to be
    inside the window, or the declaration protects nothing."""
    import inspect
    raw = inspect.getsource(RF.RelayWorker._fall_back_to_tab)
    # COMMENTS STRIPPED FIRST. The first draft of this assertion matched the explanatory
    # comment above the call -- which names _open_fresh to say why the window exists -- and
    # concluded the call came before the declaration. A source assertion that reads prose is
    # asserting about prose; this repository has paid for that one before.
    src = chr(10).join(l.split("#", 1)[0] for l in raw.splitlines())
    assert "_declare_blocking" in src and "_end_blocking" in src, (
        "the fallback no longer declares its blocking window")
    open_at = src.index("_open_fresh")
    declare_at = src.index("_declare_blocking")
    end_at = src.rindex("_end_blocking")
    assert declare_at < open_at < end_at, (
        "_open_fresh is not inside the declared window")
