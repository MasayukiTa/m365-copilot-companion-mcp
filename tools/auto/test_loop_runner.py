# -*- coding: utf-8 -*-
"""The loop that was reported as done and did not exist.

autoloop.edit_and_verify is ONE iteration: apply a change across files, verify, put it all back
if it does not hold. The plan's actual deliverable was a runner with max_iter, a quality
threshold and an exit condition that is EVALUATED. Reporting the cell as the loop was an
over-claim; this is the part that was missing.

Every exit is named, because a loop that stops without saying why cannot be told apart from one
that crashed, and "it finished" is not a result.
"""
import pytest

from tools.auto import loop_runner as L


@pytest.fixture(autouse=True)
def running(monkeypatch):
    from tools.auto import autoloop
    monkeypatch.setattr(autoloop, "stop_check", lambda: "RUN")


def fake_cell(monkeypatch, results):
    """Drive the runner with scripted cell outcomes instead of a real tree."""
    from tools.auto import autoloop
    seq = iter(results)
    monkeypatch.setattr(autoloop, "edit_and_verify",
                        lambda *a, **k: next(seq))
    return seq


EDIT = [{"path": "a.py", "old": "x", "new": "y"}]
def always(_state):
    return EDIT


# -- the named exits -------------------------------------------------------------------------

def test_a_passing_verification_converges(monkeypatch):
    fake_cell(monkeypatch, [{"ok": True, "stage": "verified", "output": "3 passed"}])
    out = L.run(always, verify="pytest", max_iter=5)
    assert out["stop"] == L.CONVERGED and out["converged"] is True
    assert out["iterations"] == 1


def test_running_out_of_iterations_is_not_success(monkeypatch):
    """THE DISTINCTION THAT MATTERS MOST. Exhausting the budget and passing are different
    facts, and a runner that returns the same shape for both hides every failed loop."""
    fake_cell(monkeypatch, [{"ok": False, "output": "%d failed" % (9 - i)} for i in range(3)])
    out = L.run(always, verify="pytest", max_iter=3)
    assert out["stop"] == L.MAX_ITER
    assert out["converged"] is False
    assert out["iterations"] == 3


def test_the_quality_threshold_is_its_own_outcome(monkeypatch):
    """Reaching a threshold is not passing. Calling it converged would report a green run that
    never went green."""
    fake_cell(monkeypatch, [{"ok": False, "output": "5 failed"},
                            {"ok": False, "output": "2 failed"}])
    out = L.run(always, verify="pytest", max_iter=5, quality_threshold=2)
    assert out["stop"] == L.THRESHOLD
    assert out["converged"] is False


def test_a_flat_failure_count_stops_the_loop(monkeypatch):
    """Iterating is not helping. Two flat rounds, not one: a single flat round is common when a
    fix lands in stages, and stopping on it throws away work about to converge."""
    fake_cell(monkeypatch, [{"ok": False, "output": "4 failed"}] * 4)
    out = L.run(always, verify="pytest", max_iter=6, patience=2)
    assert out["stop"] == L.NO_PROGRESS
    assert out["iterations"] == 3        # first sets the baseline, two flat rounds spend patience


def test_progress_resets_the_patience(monkeypatch):
    fake_cell(monkeypatch, [{"ok": False, "output": "9 failed"},
                            {"ok": False, "output": "9 failed"},
                            {"ok": False, "output": "4 failed"},
                            {"ok": True, "output": "0 failed"}])
    out = L.run(always, verify="pytest", max_iter=6, patience=2)
    assert out["stop"] == L.CONVERGED


def test_an_unknown_failure_count_is_neither_progress_nor_stagnation(monkeypatch):
    """None is not zero and it is not "the same as before" either. Treating unknown as
    improvement is how a loop spends its whole budget on a runner it cannot read; treating it as
    stagnation stops a loop that may be working fine."""
    fake_cell(monkeypatch, [{"ok": False, "output": "something unparsable"}] * 4)
    out = L.run(always, verify="pytest", max_iter=4, patience=2)
    assert out["stop"] == L.MAX_ITER          # not NO_PROGRESS
    assert out["iterations"] == 4


def test_the_kill_switch_stops_between_rounds(monkeypatch):
    """Read BEFORE anything is generated or written. A loop that only checks at the end is a
    loop that cannot be stopped."""
    from tools.auto import autoloop
    calls = {"n": 0}

    def switch():
        calls["n"] += 1
        return "RUN" if calls["n"] < 2 else "STOP (operator)"

    monkeypatch.setattr(autoloop, "stop_check", switch)
    fake_cell(monkeypatch, [{"ok": False, "output": "3 failed"}])
    out = L.run(always, verify="pytest", max_iter=5)
    assert out["stop"] == L.STOPPED
    assert out["iterations"] == 1


def test_no_further_candidate_is_an_outcome_not_an_error(monkeypatch):
    fake_cell(monkeypatch, [{"ok": False, "output": "2 failed"}])
    seq = iter([EDIT, None])
    out = L.run(lambda s: next(seq), verify="pytest", max_iter=5)
    assert out["stop"] == L.STUCK


def test_a_raising_candidate_is_recorded_and_stops_the_loop(monkeypatch):
    def boom(_s):
        raise RuntimeError("no idea")
    out = L.run(boom, verify="pytest", max_iter=3)
    assert out["stop"] == L.STUCK
    assert "RuntimeError" in out["history"][0]["error"]


# -- the budget ------------------------------------------------------------------------------

def test_there_is_no_unlimited_setting(monkeypatch):
    """The risk this module carries is an unbounded loop against a metered upstream: the tenant
    allows 100 generative messages a minute and a runaway spends them on nothing. Zero and
    negative clamp to one round rather than meaning 'forever'."""
    fake_cell(monkeypatch, [{"ok": False, "output": "1 failed"}] * 3)
    for bad in (0, -1):
        out = L.run(always, verify="pytest", max_iter=bad)
        assert out["iterations"] == 1


# -- what the caller is told -------------------------------------------------------------------

def test_every_stop_reason_has_a_sentence():
    """A stop reason only a reader of this file understands is one nobody acts on."""
    for stop in (L.CONVERGED, L.THRESHOLD, L.MAX_ITER, L.NO_PROGRESS, L.STOPPED, L.STUCK):
        for ja in (False, True):
            text = L.describe({"stop": stop, "iterations": 2}, ja=ja)
            assert text and "2" in text
    assert "did NOT converge" in L.describe({"stop": L.MAX_ITER, "iterations": 2})


def test_the_trajectory_is_returned_not_just_the_verdict(monkeypatch):
    """A loop that reports only its last state cannot be asked whether it was getting better."""
    fake_cell(monkeypatch, [{"ok": False, "output": "9 failed"},
                            {"ok": False, "output": "4 failed"},
                            {"ok": True, "output": "0 failed"}])
    out = L.run(always, verify="pytest", max_iter=5)
    assert [h["fails"] for h in out["history"]] == [9, 4, 0]


def test_custom_state_reaches_the_candidate(monkeypatch):
    """The plan named custom_state as an argument, and it is how a caller carries its own
    context between rounds without a global."""
    seen = {}
    fake_cell(monkeypatch, [{"ok": True, "output": "0 failed"}])

    def cand(state):
        seen.update(state["custom"])
        seen["iteration"] = state["iteration"]
        return EDIT

    L.run(cand, verify="pytest", max_iter=2, custom_state={"repo_kind": "go"})
    assert seen["repo_kind"] == "go" and seen["iteration"] == 1
