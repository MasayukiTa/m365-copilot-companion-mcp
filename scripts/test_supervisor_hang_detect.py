"""Hermetic, pure, Linux-CI-safe tests for scripts/supervisor_hang_detect.decide_hang_restart.

No I/O, no clock, no process calls, no Windows-specific anything: the function under test is
pure, so every case is just inputs -> one verdict word. Mirrors the shape of
test_supervisor_runner_respawn.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supervisor_hang_detect import (  # noqa: E402
    decide_hang_restart,
    HANG_THRESHOLD_SECONDS,
)


# ---- the one case this gate exists for: a wedged, silent server ---------------------------

def test_silent_past_window_while_held_by_our_live_process_restarts():
    assert decide_hang_restart(
        last_ok_age_s=120, listening=True, own_main_py_alive=True, booting=False
    ) == "restart"


def test_silence_well_past_window_still_restarts():
    assert decide_hang_restart(
        last_ok_age_s=600, listening=True, own_main_py_alive=True, booting=False
    ) == "restart"


def test_default_window_is_two_minutes():
    assert HANG_THRESHOLD_SECONDS == 120
    # exactly at the boundary counts as hung (>=)
    assert decide_hang_restart(120, True, True, False) == "restart"
    # one second short does not
    assert decide_hang_restart(119, True, True, False) == "wait"


# ---- a merely slow or recently-answered server is never killed -----------------------------

def test_recent_answer_waits():
    assert decide_hang_restart(0, True, True, False) == "wait"
    assert decide_hang_restart(16, True, True, False) == "wait"   # measured slow-but-alive max
    assert decide_hang_restart(55, True, True, False) == "wait"   # within LIVENESS_CONFIRM margin


def test_just_below_window_waits():
    assert decide_hang_restart(119.9, True, True, False) == "wait"


# ---- never acts on a server that has not had a chance (booting / never answered) ----------

def test_booting_never_restarts_even_if_silent_long():
    assert decide_hang_restart(
        last_ok_age_s=300, listening=True, own_main_py_alive=True, booting=True
    ) == "wait"


def test_never_answered_waits():
    # None = /health has never returned 200 since tracking began -> boot/vanished paths own it
    assert decide_hang_restart(None, True, True, False) == "wait"
    assert decide_hang_restart(None, True, True, True) == "wait"
    assert decide_hang_restart(None, False, False, False) == "wait"


# ---- defer to the vanished path: nothing listening is not this gate's job -----------------

def test_not_listening_waits_even_if_silent_long():
    # nothing on the port = vanished; Get-VanishedServerVerdict relaunches faster than this
    assert decide_hang_restart(
        last_ok_age_s=300, listening=False, own_main_py_alive=False, booting=False
    ) == "wait"
    assert decide_hang_restart(
        last_ok_age_s=300, listening=False, own_main_py_alive=True, booting=False
    ) == "wait"


# ---- a foreign listener is not ours to PID-kill -------------------------------------------

def test_foreign_listener_waits():
    # listening, but not a main.py of ours -> Start-Server's identity rules, not this gate
    assert decide_hang_restart(
        last_ok_age_s=300, listening=True, own_main_py_alive=False, booting=False
    ) == "wait"


# ---- fail safe on unreadable inputs: never manufacture a kill ------------------------------

def test_unreadable_age_waits():
    assert decide_hang_restart("not-a-number", True, True, False) == "wait"
    assert decide_hang_restart(object(), True, True, False) == "wait"


def test_unreadable_or_nonpositive_window_waits():
    assert decide_hang_restart(300, True, True, False, hang_threshold_s="nope") == "wait"
    assert decide_hang_restart(300, True, True, False, hang_threshold_s=0) == "wait"
    assert decide_hang_restart(300, True, True, False, hang_threshold_s=-5) == "wait"


def test_custom_window_is_honoured():
    # a caller may tighten or widen the window; the boundary tracks it
    assert decide_hang_restart(90, True, True, False, hang_threshold_s=90) == "restart"
    assert decide_hang_restart(89, True, True, False, hang_threshold_s=90) == "wait"


# ---- a simulated run: silence accumulates, then one restart, then the clock resets ---------

def test_simulated_accumulating_silence_then_reset():
    # ticks every 15s; /health silent from t=0. restart the first tick at or past 120s.
    restarts = 0
    last_ok_age = 0
    for tick in range(1, 21):          # 20 ticks = 300s
        last_ok_age = tick * 15        # 15, 30, ... 300
        verdict = decide_hang_restart(last_ok_age, True, True, False)
        if verdict == "restart":
            restarts += 1
            last_ok_age = 0            # a restart that brings /health back resets the clock
            break
    assert restarts == 1
    # first restart lands at the 8th tick (120s), not before
    assert tick == 8
