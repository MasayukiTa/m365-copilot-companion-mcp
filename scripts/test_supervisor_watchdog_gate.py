"""Hermetic unit tests for scripts/supervisor_watchdog_gate.decide_watchdog_action.

The watchdog is the out-of-process backstop for scripts/supervisor.ps1 (itself the single
point of failure for every automatic recovery on this machine). decide_watchdog_action is the
PURE decision core it relies on: given how old the supervisor's heartbeat is and whether a
supervisor process is alive, it returns 'relaunch' or 'wait'. These tests pin the decision
table so the SAFE direction cannot silently flip.

The danger to avoid is relaunching start_all while a HEALTHY supervisor is already running
(two supervisors fighting over the server and the tunnel). So the rule is deliberately
conservative: 'relaunch' happens ONLY when the supervisor is known NOT alive AND its heartbeat
is stale or absent; every ambiguous, unreadable or healthy input must fail to 'wait'.

Stdlib only, no clock, no process, no OS-specific calls: this runs in the Linux CI test job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parent / "supervisor_watchdog_gate.py"
_spec = importlib.util.spec_from_file_location("supervisor_watchdog_gate", _GATE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
decide_watchdog_action = _mod.decide_watchdog_action
STALE = _mod.STALE_THRESHOLD_SECONDS


# --- relaunch: the only direction that acts, and only when the supervisor is gone ----------

def test_dead_and_stale_heartbeat_relaunches():
    # supervisor not alive, last heartbeat older than the window -> bring it back.
    assert decide_watchdog_action(STALE + 1, False) == "relaunch"


def test_dead_and_no_heartbeat_relaunches():
    # supervisor not alive, heartbeat file absent/unparseable -> bring it back.
    assert decide_watchdog_action(None, False) == "relaunch"


def test_dead_and_far_past_window_relaunches():
    assert decide_watchdog_action(100000, False) == "relaunch"


# --- wait: a live supervisor always wins, whatever the heartbeat says -----------------------

def test_alive_with_stale_heartbeat_waits():
    # A live process wins even if the heartbeat looks old (e.g. one slow loop). Never relaunch
    # on top of a running supervisor.
    assert decide_watchdog_action(STALE + 1, True) == "wait"


def test_alive_with_no_heartbeat_waits():
    assert decide_watchdog_action(None, True) == "wait"


def test_alive_and_fresh_heartbeat_waits():
    assert decide_watchdog_action(1, True) == "wait"


# --- wait: dead but heartbeat is still fresh (it only just stopped) -------------------------

def test_dead_but_fresh_heartbeat_waits():
    # Within the window: give it the grace period. A single missed beat is not a death.
    assert decide_watchdog_action(STALE - 1, False) == "wait"


def test_dead_exactly_at_threshold_relaunches():
    # age >= window is stale (boundary is inclusive on the stale side).
    assert decide_watchdog_action(STALE, False) == "relaunch"


def test_dead_just_under_threshold_waits():
    assert decide_watchdog_action(STALE - 0.001, False) == "wait"


# --- wait: ambiguous liveness fails safe ---------------------------------------------------

def test_unknown_liveness_waits_even_when_stale():
    # supervisor_alive None means 'cannot tell' -> treated as alive -> never relaunch.
    assert decide_watchdog_action(STALE + 1, None) == "wait"


def test_unknown_liveness_waits_with_no_heartbeat():
    assert decide_watchdog_action(None, None) == "wait"


# --- wait: unreadable / nonsensical inputs fail safe ---------------------------------------

@pytest.mark.parametrize("bad_age", ["abc", "", object(), [1]])
def test_unparseable_age_waits(bad_age):
    # A heartbeat age we cannot turn into a number is not evidence of death.
    assert decide_watchdog_action(bad_age, False) == "wait"


@pytest.mark.parametrize("bad_window", [0, -1, -180, "x", None, [1]])
def test_nonpositive_or_unparseable_window_waits(bad_window):
    # A broken threshold must not make everything look stale.
    assert decide_watchdog_action(100000, False, stale_threshold_s=bad_window) == "wait"


def test_custom_window_is_honoured():
    # A caller may pass a different window; the boundary still holds against it.
    assert decide_watchdog_action(59, False, stale_threshold_s=60) == "wait"
    assert decide_watchdog_action(60, False, stale_threshold_s=60) == "relaunch"


def test_numeric_strings_are_accepted():
    # Values handed in from a shell are often strings; a numeric string must parse.
    assert decide_watchdog_action("999999", False) == "relaunch"
    assert decide_watchdog_action("1", False) == "wait"


def test_default_window_is_three_minutes():
    # The 3-minute promise in the task and the module docstring, pinned.
    assert STALE == 180
