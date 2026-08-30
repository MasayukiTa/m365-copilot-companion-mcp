"""The floor computed against a grader, and its refusal to answer when it cannot.

bench/retry_floor.py has carried a warning since it was written: DONE is the worker saying it
finished, and nothing external checked. Two independent reviews of the mechanism-usage data
named that as the central defect -- every mechanism in the system was accepted against a
self-report whose measured precision is 71.8%.

The first graded slice made a second floor computable. It also made visible why the obvious
version of it is wrong.
"""
import io
import json
import os

import pytest

from bench.retry_floor import _UNKNOWN, _graded, graded_curve, report


def _rec(goal, outcome="DONE"):
    return {"goal": goal, "outcome": outcome, "key": goal, "ts": 1.0}


def test_it_refuses_when_every_attempt_shares_one_verdict():
    """THE REFUSAL IS THE POINT.

    One patch is captured per instance, so every attempt of a goal joins to the same verdict
    and a per-k curve is flat by construction. Measured on the real slice, k=1 and k=2 both
    came out 0.755 with an identical numerator and denominator -- which reads as 'retry adds
    nothing' and actually means the question was never asked."""
    wt = {"inst-a": "C:\\w\\p00"}
    verdicts = {"inst-a": True}
    attempts = {"g1": [_rec("fix at C:\\w\\p00"), _rec("fix at C:\\w\\p00")]}
    out = graded_curve(attempts, verdicts, wt)
    assert isinstance(out, dict) and out.get("refused") is True
    assert "same verdict" in out["reason"]
    assert "per ATTEMPT" in out["what_would_unlock_it"]


def test_an_instance_outside_the_graded_slice_is_unknown_not_failed():
    """Counting ungraded attempts as failures would let the SIZE of the grading effort move
    the floor, which is the population trap this file already carries a warning about."""
    wt = {"inst-a": "C:\\w\\p00"}
    verdicts = {"inst-a": True}
    assert _graded(_rec("fix at C:\\w\\p99"), verdicts, wt) is _UNKNOWN
    assert _graded(_rec("fix at C:\\w\\p00"), verdicts, wt) is True


def test_an_ambiguous_join_is_unknown():
    """A goal naming two checkouts must not lend its attempts to one of them."""
    wt = {"a": "C:\\w\\p00", "b": "C:\\w\\p01"}
    assert _graded(_rec("both C:\\w\\p00 and C:\\w\\p01"), {"a": True, "b": False}, wt) is _UNKNOWN


def test_the_completion_floor_is_still_reported_beside_it():
    """Both floors, so the gap between them is visible. That gap is the false-DONE tax."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hist = os.path.join(here, ".fleet", "history.json")
    if not os.path.exists(hist):
        pytest.skip("no ledger on this machine")
    r = report(hist)
    assert "curve" in r and "graded_curve" in r
    assert "completion" in r["measures"]


def test_the_selection_bias_note_records_which_way_it_runs():
    """Both reviews made the same correction: the retried group is the DISADVANTAGED one --
    its first attempt was detected as failed -- and it still scores higher. The bias
    understates retry. The sharper point is about the group that is never retried, where
    false-DONE settles."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hist = os.path.join(here, ".fleet", "history.json")
    if not os.path.exists(hist):
        pytest.skip("no ledger on this machine")
    r = report(hist)
    assert r.get("the_unretried_group_is_where_false_done_settles") is True
