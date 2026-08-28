"""The watchdog must be armed by the sweep, not by a progress figure.

It used to decide whether a run was live by reading `running` out of the status file this
same process had just written. That is a progress number, not a liveness one: a fan-out parent
goes terminal at turn 1, so `running` went false for the hour its children then took, and the
wedge detector switched itself off at the moment the run began its real work. A token capture
then blocked the main loop for ten minutes -- caught with py-spy -- and nothing fired.

`running` has since been fixed to count queued work, but the SHAPE of the mistake survives it:
a narrower window still exists between the last child finishing and the merge being queued.
The fact being asked about is whether the sweep loop is executing, and that is available
in-process.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "relay", "fleet_runner.py")


def _watchdog_body():
    src = open(SRC, encoding="utf-8").read()
    i = src.index("    def _watchdog():")
    return src[i:src.index("threading.Thread(target=_watchdog", i)]


def test_liveness_comes_from_the_sweep_not_the_status_file():
    body = _watchdog_body()
    assert "sweep_active.is_set()" in body
    assert 'not d.get("running")' not in body, \
        "a progress figure must not decide whether the wedge detector is armed"


def test_the_idle_flag_is_still_honoured():
    """Idle is a real state -- a paused fleet is not a wedged one."""
    assert 'd.get("idle")' in _watchdog_body()


def test_the_sweep_arms_and_disarms_around_the_run():
    src = open(SRC, encoding="utf-8").read()
    arm = src.index("sweep_active.set()")
    call = src.index("run_relay_fleet(context", arm)
    assert arm < call, "armed before the sweep starts"
    assert src.count("sweep_active.clear()") >= 2, \
        "must disarm on the normal exit AND on the context-lost path"


def test_it_disarms_before_the_teardown():
    """Result mapping, memory recording, notifications and tab teardown can outlast stall_s.
    A watchdog still armed would hard-reset the browser out from under the cleanup that is
    finishing the run."""
    # ORDER, NOT PROXIMITY. This read a 400-character window after the disarm and looked for
    # the result loop in it. A comment added above that loop pushed it out of the window and
    # the test failed while the order it cares about was unchanged -- the third character-
    # window test to break that way in this repository.
    #
    # What it means to assert is that the disarm happens BEFORE the results are consumed.
    # That is an ordering, and an ordering is comparable positions, not a distance.
    src = open(SRC, encoding="utf-8").read()
    clear = src.index("sweep_active.clear()")
    consume = src.index("for r in res:")
    assert clear < consume, "the disarm must come before the results are consumed"


def test_stop_wd_alone_is_not_used_as_liveness():
    """stop_wd is cleared only after the final cleanup, so it would leave the watchdog armed
    through exactly the teardown it must not interrupt."""
    body = _watchdog_body()
    i = body.index("sweep_active.is_set()")
    assert "stop_wd.is_set()" in body[:i], "the loop still exits on stop_wd"
