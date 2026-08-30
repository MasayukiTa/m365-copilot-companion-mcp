"""The cross-tabulation of a self-report against an oracle.

bench/retry_floor.py has carried a warning since it was written: DONE is the worker saying it
finished, and nothing external checked. Every number built on it measures a claim. A graded
slice is the first oracle available, and this is what the two facts look like side by side.
"""
import io
import json

import pytest

from bench.done_vs_correct import attempts_by_instance, report


@pytest.fixture
def files(tmp_path):
    ev = tmp_path / "eval_results.json"
    hist = tmp_path / "history.json"
    return ev, hist


def _write(ev, hist, verdicts, rows):
    io.open(ev, "w", encoding="utf-8").write(json.dumps(verdicts))
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))


def test_the_headline_is_how_often_done_was_right(files):
    """Two instances claimed DONE; one was actually resolved."""
    ev, hist = files
    _write(ev, hist,
           {"a__a-1": True, "b__b-2": False},
           [{"goal": "fix a__a-1 please", "outcome": "DONE"},
            {"goal": "fix b__b-2 please", "outcome": "DONE"}])
    r = report(str(ev), str(hist))
    assert r["said_done_and_correct"] == 1
    assert r["said_done_and_wrong"] == 1
    assert r["precision_of_done"] == 0.5
    assert r["said_done_and_wrong_ids"] == ["b__b-2"]


def test_an_instance_nobody_claimed_still_counts_in_the_grade(files):
    """Resolved without a DONE is a real cell, not an anomaly to drop."""
    ev, hist = files
    _write(ev, hist,
           {"a__a-1": True},
           [{"goal": "fix a__a-1", "outcome": "STUCK"}])
    r = report(str(ev), str(hist))
    assert r["never_said_done_but_correct"] == 1
    assert r["precision_of_done"] is None, "no DONE claims means no precision to report"


def test_an_ambiguous_join_attaches_to_neither(files):
    """A goal naming two instances must not lend its attempts to one of them.

    Silently attributing one worker's attempts to another instance is worse than a missing
    join, because the resulting number looks complete."""
    ev, hist = files
    _write(ev, hist,
           {"a__a-1": True, "a__a-12": False},
           [{"goal": "fix a__a-1 and a__a-12", "outcome": "DONE"}])
    att = attempts_by_instance(json.load(io.open(hist, encoding="utf-8")),
                               ["a__a-1", "a__a-12"])
    assert att == {}, "an ambiguous goal was joined anyway"


def test_the_resolved_rate_is_over_graded_instances_not_over_ledger_rows(files):
    """One population, one fraction. The rate above 1 that this rule exists for came from
    dividing a count over all goals by a count over eligible goals."""
    ev, hist = files
    _write(ev, hist,
           {"a__a-1": True, "b__b-2": False, "c__c-3": True},
           [{"goal": "fix a__a-1", "outcome": "DONE"},
            {"goal": "fix a__a-1", "outcome": "DONE"}])
    r = report(str(ev), str(hist))
    assert r["instances_graded"] == 3
    assert r["resolved"] == 2
    assert abs(r["resolved_rate"] - 2 / 3) < 1e-9
    assert r["instances_with_ledger_attempts"] == 1


def test_it_says_out_loud_that_it_is_not_pass_at_k(files):
    """One patch is captured per instance, so there is one graded attempt each. A reader who
    takes the attempt-count table for pass@k gets a confident wrong answer."""
    ev, hist = files
    _write(ev, hist, {"a__a-1": True}, [])
    r = report(str(ev), str(hist))
    assert "not_pass_at_k" in r and "one graded attempt" in r["not_pass_at_k"]
