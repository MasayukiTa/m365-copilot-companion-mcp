# -*- coding: utf-8 -*-
"""The loop whose depth is decided while it runs.

`loop_until_verified` -- the only exposed caller of loop_runner.run -- takes `edits_per_round`
and indexes it by iteration, so round N's content must exist before round 1 starts and the loop
stops as `stuck` when the list runs out. Depth is therefore a property of the caller's list, not
of what the verification said. These tests pin the inverted shape: control returns between
rounds, so each round is generated after seeing the last one's verdict.

Hermetic: the runlog directory is redirected, the cell is scripted, and no command is run.
"""
from __future__ import annotations

import json

import pytest

from tools import recurrent_ops as R
from tools.auto import autoloop, loop_runner as L


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """No gate, no kill switch, and the runlog under tmp_path rather than the real home."""
    from tools import runlog_ops
    monkeypatch.setattr(runlog_ops, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(R, "require_unlocked", lambda: None)
    monkeypatch.setattr(autoloop, "stop_check", lambda: "RUN")


def cell(monkeypatch, results):
    """Script the round outcomes instead of touching a tree."""
    seq = iter(results)
    monkeypatch.setattr(autoloop, "edit_and_verify", lambda *a, **k: next(seq))


EDIT = [{"path": "a.py", "old": "x", "new": "y"}]
PASS = {"ok": True, "stage": "verified", "exit_code": 0, "output": "3 passed"}
BINARY_FAIL = {"ok": False, "stage": "verify", "exit_code": 1, "output": "HIDDEN_TESTS_FAILED"}


def counted(n):
    return {"ok": False, "stage": "verify", "exit_code": 1, "output": "%d failed" % n}


def begin(**kw):
    kw.setdefault("goal", "make the tests pass")
    kw.setdefault("run_id", "t1")
    return json.loads(R.recurrent_begin(**kw))


def step(edits=EDIT, run_id="t1"):
    return json.loads(R.recurrent_step(run_id, edits))


# -- the property loop_until_verified cannot have ---------------------------------------------

def test_no_edits_are_supplied_up_front(monkeypatch):
    """THE POINT. Opening the loop takes a goal, not a list of rounds -- so round 2 can be
    written after round 1's verdict is known, which is what test-time depth means."""
    out = begin(max_iter=4)
    assert out["iterations_done"] == 0
    assert out["next_iteration"] == 1
    assert out["stop"] == R.CONTINUE
    assert "edits" not in out


def test_each_round_hands_back_the_verdict_before_the_next_is_generated(monkeypatch):
    cell(monkeypatch, [counted(5), counted(3), PASS])
    begin(max_iter=5, patience=3)
    a = step()
    assert a["stop"] == R.CONTINUE and a["history"][-1]["fails"] == 5
    b = step()
    assert b["stop"] == R.CONTINUE and b["history"][-1]["fails"] == 3
    c = step()
    assert c["stop"] == L.CONVERGED and c["iterations_done"] == 3


def test_the_band_changes_with_depth(monkeypatch):
    """Round 1-2 explore, 3+ refine. The band is returned; the wording that goes with it is a
    separate mapping, so this file does not change when the wording does."""
    assert [R.depth_band(i) for i in (1, 2, 3, 9)] == [R.BAND_EXPLORE, R.BAND_EXPLORE,
                                                       R.BAND_REFINE, R.BAND_REFINE]
    cell(monkeypatch, [counted(5), counted(4)])
    assert begin(max_iter=5, patience=9)["depth_band"] == R.BAND_EXPLORE
    step()
    assert step()["depth_band"] == R.BAND_REFINE


# -- the state survives the caller, because the log is the state ------------------------------

def test_progress_is_recomputed_from_the_log_not_carried_in_memory(monkeypatch):
    """Each round arrives as a separate call with nothing carried between them, so patience has
    to be reconstructible from what was written down."""
    cell(monkeypatch, [counted(5), counted(7)])
    begin(max_iter=5, patience=9)
    step()
    step()
    out = json.loads(R.recurrent_state("t1"))
    assert out["best_failures"] == 5
    assert out["rounds_without_improvement"] == 1
    assert [h["fails"] for h in out["history"]] == [5, 7]


def test_reading_an_unknown_run_says_so_rather_than_inventing_one():
    assert "no such run" in R.recurrent_state("nope")
    assert "no such run" in R.recurrent_step("nope", EDIT)


def test_reopening_a_live_run_is_refused_not_silently_restarted():
    begin()
    again = R.recurrent_begin(goal="something else", run_id="t1")
    assert "already exists" in again


def test_a_settled_loop_does_not_take_another_round(monkeypatch):
    cell(monkeypatch, [PASS])
    begin()
    assert step()["stop"] == L.CONVERGED
    assert "already stopped" in R.recurrent_step("t1", EDIT)


# -- every exit is named ----------------------------------------------------------------------

def test_empty_edits_are_stuck_which_is_an_outcome(monkeypatch):
    begin()
    out = step(edits=[])
    assert out["stop"] == L.STUCK
    assert "nothing further" in out["reason"]


def test_the_kill_switch_is_read_before_the_round_is_applied(monkeypatch):
    applied = []
    monkeypatch.setattr(autoloop, "stop_check", lambda: "STOP")
    monkeypatch.setattr(autoloop, "edit_and_verify",
                        lambda *a, **k: applied.append(1) or PASS)
    begin()
    assert step()["stop"] == L.STOPPED
    assert applied == [], "the tree was edited after the switch said stop"


def test_running_out_of_budget_is_not_success(monkeypatch):
    cell(monkeypatch, [counted(5), counted(4)])
    begin(max_iter=2, patience=9)
    step()
    out = step()
    assert out["stop"] == L.MAX_ITER and out["reason"] == "budget spent"


def test_reaching_the_threshold_is_reported_apart_from_passing(monkeypatch):
    """A green run that never went green must not be called converged."""
    cell(monkeypatch, [counted(2)])
    begin(quality_threshold=2)
    assert step()["stop"] == L.THRESHOLD


def test_a_counting_runner_stops_on_patience(monkeypatch):
    """patience=2 means TWO rounds without improvement, so the first flat round is not enough --
    the same off-by-one that makes a single flat round a bad reason to give up, which is why
    loop_runner's default is 2 and says so."""
    cell(monkeypatch, [counted(5), counted(5), counted(5)])
    begin(max_iter=9, patience=2)
    assert step()["stop"] == R.CONTINUE          # best = 5
    assert step()["stop"] == R.CONTINUE          # flat = 1, still under patience
    out = step()                                 # flat = 2
    assert out["stop"] == L.NO_PROGRESS
    assert out["rounds_without_improvement"] == 2


# -- the binary verifier, which is what 178 of the 188 checked goals actually use --------------

def test_by_default_a_binary_runner_still_spends_the_whole_budget(monkeypatch):
    """count_failures answers None for HIDDEN_TESTS_FAILED, so no round can be improvement and
    none can be stagnation. Pinned, not endorsed: moving this default is a measured trade."""
    cell(monkeypatch, [dict(BINARY_FAIL) for _ in range(4)])
    begin(max_iter=4, patience=2)
    for _ in range(3):
        step()
    out = step()
    assert out["stop"] == L.MAX_ITER
    assert out["iterations_done"] == 4
    assert {h["signal"] for h in out["history"]} == {autoloop.SIGNAL_FAIL}


def test_with_binary_patience_a_reported_failure_counts_as_no_improvement(monkeypatch):
    cell(monkeypatch, [dict(BINARY_FAIL) for _ in range(4)])
    begin(max_iter=4, patience=2, binary_patience=True)
    step()
    out = step()
    assert out["stop"] == L.NO_PROGRESS
    assert out["iterations_done"] == 2


def test_binary_patience_does_not_fire_on_a_timeout(monkeypatch):
    """A command that never finished reported nothing; giving up on it would mistake an
    infrastructure fault for a stalled search."""
    timeout = {"ok": False, "stage": "timeout", "exit_code": None, "output": ""}
    cell(monkeypatch, [dict(timeout) for _ in range(3)])
    begin(max_iter=3, patience=2, binary_patience=True)
    step()
    step()
    out = step()
    assert out["stop"] == L.MAX_ITER
    assert {h["signal"] for h in out["history"]} == {autoloop.SIGNAL_UNKNOWN}


# -- the gate ---------------------------------------------------------------------------------

def test_the_mutating_calls_are_gated(monkeypatch):
    monkeypatch.setattr(R, "require_unlocked", lambda: "[locked: no]")
    assert R.recurrent_begin(goal="g", run_id="t9").startswith("[locked")
    assert R.recurrent_step("t9", EDIT).startswith("[locked")
