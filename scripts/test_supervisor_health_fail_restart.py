"""Hermetic, pure, Linux-CI-safe tests for scripts/supervisor_health_fail_restart.

No I/O, no clock, no process calls, no Windows-specific anything: the functions under test
are pure, so every case is inputs -> one verdict/word/number. Mirrors the shape of
test_supervisor_hang_detect.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supervisor_health_fail_restart import (  # noqa: E402
    classify_health_check,
    next_streak,
    decide_health_fail_restart,
    SLOW_HEALTH_BOUND_SECONDS,
    SLOW_HEALTH_FAIL_STREAK,
)


# ---- the measured constants are what the code comment claims ------------------------------

def test_measured_constants():
    assert SLOW_HEALTH_BOUND_SECONDS == 30   # clears the measured 16s slow-but-alive max
    assert SLOW_HEALTH_FAIL_STREAK == 4      # matches FailuresBeforeAction debounce


# ---- classify one check: fail OR slow == unhealthy; fast answer == healthy ----------------

def test_no_answer_is_unhealthy():
    assert classify_health_check(answered=False, seconds=None) == "unhealthy"
    assert classify_health_check(answered=False, seconds=99) == "unhealthy"


def test_fast_answer_is_healthy():
    assert classify_health_check(answered=True, seconds=0) == "healthy"
    assert classify_health_check(answered=True, seconds=3) == "healthy"   # the "slow (3s)" log case
    assert classify_health_check(answered=True, seconds=16) == "healthy"  # measured slow-but-alive max
    assert classify_health_check(answered=True, seconds=29.9) == "healthy"


def test_answer_at_or_past_bound_is_unhealthy():
    assert classify_health_check(answered=True, seconds=30) == "unhealthy"   # boundary is >=
    assert classify_health_check(answered=True, seconds=55) == "unhealthy"
    assert classify_health_check(answered=True, seconds=120) == "unhealthy"


def test_custom_bound_is_honoured():
    assert classify_health_check(True, 20, bound_s=20) == "unhealthy"
    assert classify_health_check(True, 19, bound_s=20) == "healthy"


def test_unreadable_timer_fails_safe_to_healthy():
    # an answered check with a broken timer must NOT become a fault
    assert classify_health_check(True, "not-a-number") == "healthy"
    assert classify_health_check(True, object()) == "healthy"


def test_nonpositive_or_unreadable_bound_disables_slowness_arm():
    # any answer is healthy when the bound is broken (never turn every tick into a fault)
    assert classify_health_check(True, 999, bound_s=0) == "healthy"
    assert classify_health_check(True, 999, bound_s=-5) == "healthy"
    assert classify_health_check(True, 999, bound_s="nope") == "healthy"
    # but a NON-answer is still unhealthy regardless of the bound
    assert classify_health_check(False, None, bound_s=0) == "unhealthy"


# ---- streak accounting: unhealthy increments, a single healthy check resets ---------------

def test_streak_increments_on_unhealthy():
    assert next_streak(0, answered=False, seconds=None) == 1
    assert next_streak(1, answered=True, seconds=40) == 2
    assert next_streak(3, answered=False, seconds=None) == 4


def test_one_healthy_check_resets_the_streak():
    assert next_streak(3, answered=True, seconds=3) == 0
    assert next_streak(10, answered=True, seconds=0) == 0


def test_damaged_prev_streak_treated_as_zero():
    assert next_streak("bad", answered=False, seconds=None) == 1
    assert next_streak(-5, answered=False, seconds=None) == 1
    assert next_streak(None, answered=True, seconds=3) == 0


# ---- the restart decision: N in a row, process alive, not booting, no coordinator ---------

def test_restart_at_streak_n_when_held_by_our_live_process():
    assert decide_health_fail_restart(
        streak=4, listening=True, own_main_py_alive=True, booting=False
    ) == "restart"


def test_streak_below_n_waits():
    for s in (0, 1, 2, 3):
        assert decide_health_fail_restart(
            streak=s, listening=True, own_main_py_alive=True, booting=False
        ) == "wait"


def test_streak_well_past_n_still_restarts():
    assert decide_health_fail_restart(9, True, True, False) == "restart"


def test_custom_n_is_honoured():
    assert decide_health_fail_restart(2, True, True, False, fail_streak_n=2) == "restart"
    assert decide_health_fail_restart(1, True, True, False, fail_streak_n=2) == "wait"


# ---- never kill a booting / vanished / foreign server -------------------------------------

def test_booting_never_restarts():
    assert decide_health_fail_restart(99, True, True, True) == "wait"


def test_not_listening_waits():
    # vanished: Get-VanishedServerVerdict relaunches faster than this
    assert decide_health_fail_restart(99, False, False, False) == "wait"
    assert decide_health_fail_restart(99, False, True, False) == "wait"


def test_foreign_listener_waits():
    assert decide_health_fail_restart(99, True, False, False) == "wait"


# ---- do not cut a live fleet run on slowness alone ----------------------------------------

def test_coordinator_running_defers():
    assert decide_health_fail_restart(
        99, True, True, False, coordinator_running=True
    ) == "wait"


# ---- fail safe on unreadable / non-positive inputs ----------------------------------------

def test_unreadable_streak_waits():
    assert decide_health_fail_restart("bad", True, True, False) == "wait"
    assert decide_health_fail_restart(object(), True, True, False) == "wait"


def test_nonpositive_or_unreadable_n_waits():
    assert decide_health_fail_restart(99, True, True, False, fail_streak_n=0) == "wait"
    assert decide_health_fail_restart(99, True, True, False, fail_streak_n=-1) == "wait"
    assert decide_health_fail_restart(99, True, True, False, fail_streak_n="nope") == "wait"


# ---- a simulated run: slow-but-answering accumulates, then exactly one restart at N --------

def test_simulated_slow_but_answering_restarts_at_fourth_tick():
    # the 2026-11-11 shape: /health keeps answering, but always past the 30s bound.
    streak = 0
    restarts = 0
    fired_tick = None
    for tick in range(1, 11):
        streak = next_streak(streak, answered=True, seconds=35)   # answered, but slow every tick
        verdict = decide_health_fail_restart(streak, True, True, False)
        if verdict == "restart":
            restarts += 1
            fired_tick = tick
            streak = 0   # a restart that brings /health back resets the streak
            break
    assert restarts == 1
    assert fired_tick == 4   # four consecutive slow checks, as SLOW_HEALTH_FAIL_STREAK says


def test_one_fast_tick_in_the_middle_prevents_the_restart():
    # a single healthy tick resets the streak, so a server that recovers is never killed
    streak = 0
    seq = [35, 35, 3, 35, 35]   # slow, slow, FAST, slow, slow -> never reaches 4 in a row
    saw_restart = False
    for s in seq:
        streak = next_streak(streak, answered=True, seconds=s)
        if decide_health_fail_restart(streak, True, True, False) == "restart":
            saw_restart = True
    assert not saw_restart
