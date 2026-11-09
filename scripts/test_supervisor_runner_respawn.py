# -*- coding: utf-8 -*-
r"""The supervisor must restart a dead fleet runner that still has unfinished work.

WHY THIS TEST EXISTS (design gap measured 2026-11-10)
-----------------------------------------------------
relay/fleet_runner.py exited at ~04:52 with an uncaught FleetRecoveryTimeout and
nobody restarted it: the supervisor restarted only main.py, and .fleet/status.json
kept saying running=true, recovering=true, pending=33 for 18 minutes until an
unrelated new goal happened to start the runner. The fix adds a SECOND, independent
respawn trigger in scripts/supervisor.ps1 whose decision core is the pure module
scripts/supervisor_runner_respawn.py. This file pins that core's contract.

The central property the GOAL asks for is EXACTLY ONE RESTART PER BACKOFF WINDOW: a
runner that is dead-with-pending-work on every tick must be respawned once, then not
again until the backoff has elapsed, and never more than the cap inside ten minutes.

Pure: no I/O, no clock, no process, no network. Linux-CI safe.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supervisor_runner_respawn as R


# --------------------------------------------------------------- the four refusals

def test_a_live_runner_is_left_alone():
    # A runner is alive -> never respawn, whatever the work state.
    assert R.decide_runner_respawn(runner_alive=True, has_unfinished_work=True,
                                   respawn_history=[], now=1000.0) == "runner_alive"
    assert R.decide_runner_respawn(runner_alive=True, has_unfinished_work=False,
                                   respawn_history=[], now=1000.0) == "runner_alive"


def test_no_unfinished_work_means_an_absent_runner_is_expected():
    # Runner absent but nothing left to do -> the normal finished/idle state.
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=False,
                                   respawn_history=[], now=1000.0) == "no_work"


def test_an_unreadable_clock_refuses_rather_than_guesses():
    for bad in (0, 0.0, -5, "nope", None):
        assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                       respawn_history=[], now=bad) == "unknown_clock"


def test_dead_runner_with_work_and_no_history_respawns():
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=[], now=1000.0) == "respawn"


# --------------------------------------------------------------- the backoff floor

def test_backoff_floor_is_at_least_thirty_seconds():
    assert R.MIN_RESPAWN_INTERVAL_SECONDS >= 30


def test_a_respawn_inside_the_backoff_is_refused():
    # Last respawn 10s ago (< 30s floor) -> backoff, even with work pending.
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=[990.0], now=1000.0) == "backoff"


def test_a_respawn_after_the_backoff_is_allowed():
    # Last respawn 31s ago (> 30s floor), cap not reached -> respawn.
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=[969.0], now=1000.0) == "respawn"


def test_a_configured_interval_below_the_floor_is_raised_to_the_floor():
    # Even if a caller passes min_interval=5, the 30s floor still applies: 10s ago
    # must still be refused.
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=[990.0], now=1000.0,
                                   min_interval=5) == "backoff"


# --------------------------------------------------------------------- the 10m cap

def test_five_respawns_in_the_window_hit_the_cap():
    # Five respawns spread across the last ~9 minutes, the newest > 30s ago so the
    # backoff does not fire first -> the cap is what refuses.
    hist = [1000.0 - d for d in (540, 400, 300, 200, 100)]
    assert R.count_respawns_in_window(hist, 1000.0) == 5
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=hist, now=1000.0) == "cap"


def test_a_respawn_that_aged_out_of_the_window_no_longer_counts():
    # Four inside the window + one that is 11 minutes old (outside 600s) -> only four
    # count, so a fifth is allowed.
    hist = [1000.0 - d for d in (660, 400, 300, 200, 100)]
    assert R.count_respawns_in_window(hist, 1000.0) == 4
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=hist, now=1000.0) == "respawn"


# ------------------------------------------------ the central property: one per window

def test_exactly_one_respawn_per_backoff_window_on_a_dead_runner():
    """Drive a simulated supervisor loop: a runner that is dead-with-pending-work on
    EVERY tick must be respawned exactly once per backoff window, not every tick.

    The loop ticks every 15s (the supervisor's interval). The runner is permanently
    dead and there is always unfinished work -- the worst case for thrashing. We feed
    the decider the respawn history we have accumulated and record a respawn whenever
    it says so, exactly as scripts/supervisor.ps1 does.
    """
    tick = 15.0
    start = 1_000_000.0
    respawns = []
    # 40 ticks = 600 seconds = exactly one 10-minute window.
    for i in range(40):
        now = start + i * tick
        decision = R.decide_runner_respawn(
            runner_alive=False,
            has_unfinished_work=True,
            respawn_history=list(respawns),
            now=now,
        )
        if decision == "respawn":
            respawns.append(now)

    # No two respawns are closer together than the 30s floor.
    gaps = [b - a for a, b in zip(respawns, respawns[1:])]
    assert gaps, "expected at least two respawns across 600s"
    assert min(gaps) >= R.MIN_RESPAWN_INTERVAL_SECONDS

    # And never more than the cap inside any 600s window.
    for i in range(len(respawns)):
        window = [t for t in respawns if respawns[i] - R.RESPAWN_WINDOW_SECONDS < t <= respawns[i]]
        assert len(window) <= R.MAX_RESPAWNS_PER_WINDOW

    # Concretely over this single window: the cap holds the total at 5.
    assert len(respawns) == R.MAX_RESPAWNS_PER_WINDOW


def test_within_one_backoff_window_only_the_first_tick_respawns():
    """The tightest reading of 'exactly one restart per backoff window': across a run
    of ticks all inside a single 30s backoff, the decider says 'respawn' on the first
    and 'backoff' on every later tick until the backoff elapses."""
    start = 2_000_000.0
    respawns = []
    decisions = []
    # Ticks every 5s for 30s: t=0,5,10,15,20,25. Only the first should respawn;
    # the next five are inside the 30s backoff.
    for i in range(6):
        now = start + i * 5.0
        d = R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                    respawn_history=list(respawns), now=now)
        decisions.append(d)
        if d == "respawn":
            respawns.append(now)
    assert decisions[0] == "respawn"
    assert decisions[1:] == ["backoff"] * 5
    assert len(respawns) == 1


def test_the_runner_coming_back_stops_the_respawns():
    # Once a respawned runner is alive again, the decider stops acting even with work.
    assert R.decide_runner_respawn(runner_alive=True, has_unfinished_work=True,
                                   respawn_history=[999_999.0], now=1_000_050.0) == "runner_alive"


# --------------------------------------------------------------- history edge cases

def test_a_future_dated_respawn_does_not_block_a_real_one():
    # A respawn stamped in the future (clock skew) must not be read as a recent respawn
    # that suppresses action forever.
    assert R.seconds_since_last_respawn([1000.0 + 500], 1000.0) is None
    assert R.decide_runner_respawn(runner_alive=False, has_unfinished_work=True,
                                   respawn_history=[1000.0 + 500], now=1000.0) == "respawn"


def test_non_numeric_history_entries_are_ignored():
    assert R.count_respawns_in_window(["x", None, 990.0], 1000.0) == 1
    assert R.seconds_since_last_respawn(["x", None, 969.0], 1000.0) == 31.0


# ---------------------------------------------------------------------------------
# Reading unfinished work from a REAL-SHAPED status.json (the bug this PR fixes)
# ---------------------------------------------------------------------------------
#
# The samples below copy ONLY the key names from a live .fleet/status.json -- running,
# recovering, pending, queued, total, done_count, and per-worker `status` (string) and
# `closed` (bool). No goal/business text: worker names are w0..wN and statuses are the
# generic fleet state words. The point is the SHAPE: workers carry `status`+`closed`,
# NOT `done`/`state`, which is exactly what the old code got wrong.


def _worker(name, status, closed):
    return {"name": name, "status": status, "closed": closed}


def test_worker_is_finished_uses_closed_or_terminal_status():
    # closed true -> finished, whatever the status string.
    assert R.worker_is_finished(_worker("w0", "waiting", True)) is True
    # done / cancelled / stuck count as finished even when not closed.
    assert R.worker_is_finished(_worker("w1", "done", False)) is True
    assert R.worker_is_finished(_worker("w2", "cancelled", False)) is True
    assert R.worker_is_finished(_worker("w3", "stuck", False)) is True
    # open and in any other state -> unfinished.
    for st in ("waiting", "ready", "refuting", "awaiting_children"):
        assert R.worker_is_finished(_worker("wx", st, False)) is False
    # a worker that carries only the OLD keys (done/state) is unfinished -- the shape
    # we do not understand must not read as idle.
    assert R.worker_is_finished({"name": "wold", "done": True, "state": "done"}) is False


def test_all_closed_or_done_workers_means_no_unfinished_work():
    # The legitimate idle state from the GOAL: running=true but every worker is closed
    # or done -> NOT unfinished (so an absent runner is NOT respawned).
    status = {
        "running": True,
        "recovering": False,
        "pending": 0,
        "queued": 0,
        "total": 3,
        "done_count": 3,
        "workers": [
            _worker("w0", "done", True),
            _worker("w1", "stuck", True),
            _worker("w2", "done", False),
        ],
    }
    assert R.status_json_has_unfinished_work(status) is False
    # And the decider therefore leaves the absent runner alone.
    assert R.decide_runner_respawn(
        runner_alive=False,
        has_unfinished_work=R.status_json_has_unfinished_work(status),
        respawn_history=[], now=1000.0) == "no_work"


def test_one_open_waiting_worker_means_unfinished_work():
    # Same run, but one worker is open and waiting -> unfinished -> respawn.
    status = {
        "running": True,
        "recovering": False,
        "pending": 0,
        "queued": 0,
        "total": 3,
        "done_count": 2,
        "workers": [
            _worker("w0", "done", True),
            _worker("w1", "waiting", False),
            _worker("w2", "done", True),
        ],
    }
    assert R.status_json_has_unfinished_work(status) is True
    assert R.decide_runner_respawn(
        runner_alive=False,
        has_unfinished_work=R.status_json_has_unfinished_work(status),
        respawn_history=[], now=1000.0) == "respawn"


def test_recovering_true_is_unfinished_even_with_all_workers_closed():
    # recovering=true is the 18-minute stuck state in the GOAL: unfinished on its own,
    # even when every worker is closed and pending is 0.
    status = {
        "running": True,
        "recovering": True,
        "pending": 0,
        "queued": 0,
        "workers": [_worker("w0", "done", True)],
    }
    assert R.status_json_has_unfinished_work(status) is True
    assert R.decide_runner_respawn(
        runner_alive=False,
        has_unfinished_work=R.status_json_has_unfinished_work(status),
        respawn_history=[], now=1000.0) == "respawn"


def test_pending_greater_than_zero_is_unfinished():
    # running with pending>0 and no worker list -> unfinished.
    status = {"running": True, "recovering": False, "pending": 33, "queued": 0}
    assert R.status_json_has_unfinished_work(status) is True


def test_a_missing_status_file_is_not_unfinished_work():
    # status.json missing/unreadable -> the PowerShell side passes None -> no run to
    # bring back -> False (an absent runner with no state is left alone).
    assert R.status_json_has_unfinished_work(None) is False
    assert R.decide_runner_respawn(
        runner_alive=False,
        has_unfinished_work=R.status_json_has_unfinished_work(None),
        respawn_history=[], now=1000.0) == "no_work"


def test_not_running_is_not_unfinished_work():
    # running=false -> nothing is running, even if workers look open.
    status = {"running": False, "recovering": False,
              "workers": [_worker("w0", "waiting", False)]}
    assert R.status_json_has_unfinished_work(status) is False
