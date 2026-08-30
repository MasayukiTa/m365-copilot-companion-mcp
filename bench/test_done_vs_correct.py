"""The cross-tabulation of a self-report against an oracle.

bench/retry_floor.py has carried a warning since it was written: DONE is the worker saying it
finished, and nothing external checked. Every number built on it measures a claim. A graded
slice is the first oracle available, and this is what the two facts look like side by side.
"""
import io
import json

import pytest

from bench.done_vs_correct import attempts_by_instance, report, worktree_map_for


@pytest.fixture
def files(tmp_path):
    return (tmp_path / "eval_results.json", tmp_path / "history.json",
            tmp_path / "slice.json")


def _write(ev, hist, sl, verdicts, rows, ids=None):
    io.open(ev, "w", encoding="utf-8").write(json.dumps(verdicts))
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    ids = ids if ids is not None else sorted(verdicts)
    io.open(sl, "w", encoding="utf-8").write(
        json.dumps([{"instance_id": i} for i in ids]))


def _wt(sl, inst):
    """The worktree path the goal for `inst` would name."""
    return worktree_map_for(str(sl))[inst]

def test_the_headline_is_how_often_done_was_right(files):
    """Two instances claimed DONE; one was actually resolved."""
    ev, hist, sl = files
    ids = ["a__a-1", "b__b-2"]
    _write(ev, hist, sl, {"a__a-1": True, "b__b-2": False}, [], ids)
    rows = [{"goal": "checked out at %s" % _wt(sl, "a__a-1"), "outcome": "DONE"},
            {"goal": "checked out at %s" % _wt(sl, "b__b-2"), "outcome": "DONE"}]
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    r = report(str(ev), str(hist), str(sl))
    assert r["said_done_and_correct"] == 1
    assert r["said_done_and_wrong"] == 1
    assert r["precision_of_done"] == 0.5
    assert r["said_done_and_wrong_ids"] == ["b__b-2"]


def test_the_goal_names_the_checkout_not_the_instance(files):
    """THE DEFECT THIS JOIN WAS REWRITTEN FOR.

    The first version matched on the instance id appearing in the goal. Goals name the
    worktree instead, so it matched nothing: 40 instances, 0 attempts, every one filed as
    'never said DONE' -- which reads exactly like a fleet that never claimed anything."""
    ev, hist, sl = files
    _write(ev, hist, sl, {"a__a-1": True}, [], ["a__a-1"])
    rows = [{"goal": "the repository is checked out locally at: %s" % _wt(sl, "a__a-1"),
             "outcome": "DONE"}]
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    r = report(str(ev), str(hist), str(sl))
    assert r["instances_with_ledger_attempts"] == 1
    assert r["said_done_and_correct"] == 1
    # And the id itself must NOT be what makes it match.
    assert "a__a-1" not in rows[0]["goal"]


def test_a_prefix_path_does_not_match_a_longer_one(files):
    """p1 must not claim the attempts of p10. Reuses swe_run_facts' boundary rule."""
    ids = ["i-%02d" % n for n in range(12)]
    ev, hist, sl = files
    _write(ev, hist, sl, {i: True for i in ids}, [], ids)
    p10 = _wt(sl, ids[10])
    rows = [{"goal": "checked out at %s" % p10, "outcome": "DONE"}]
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    att = attempts_by_instance(json.load(io.open(hist, encoding="utf-8")),
                               worktree_map_for(str(sl)))
    assert list(att) == [ids[10]], "a shorter path claimed a longer one's attempts"


def test_an_instance_nobody_claimed_still_counts_in_the_grade(files):
    ev, hist, sl = files
    _write(ev, hist, sl, {"a__a-1": True}, [], ["a__a-1"])
    rows = [{"goal": "at %s" % _wt(sl, "a__a-1"), "outcome": "STUCK"}]
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    r = report(str(ev), str(hist), str(sl))
    assert r["never_said_done_but_correct"] == 1
    assert r["precision_of_done"] is None


def test_the_resolved_rate_is_over_graded_instances_not_over_ledger_rows(files):
    """One population, one fraction."""
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    ev, hist, sl = files
    _write(ev, hist, sl, {"a__a-1": True, "b__b-2": False, "c__c-3": True}, [], ids)
    rows = [{"goal": "at %s" % _wt(sl, "a__a-1"), "outcome": "DONE"},
            {"goal": "at %s" % _wt(sl, "a__a-1"), "outcome": "DONE"}]
    io.open(hist, "w", encoding="utf-8").write(json.dumps(rows))
    r = report(str(ev), str(hist), str(sl))
    assert r["instances_graded"] == 3 and r["resolved"] == 2
    assert abs(r["resolved_rate"] - 2 / 3) < 1e-9
    assert r["instances_with_ledger_attempts"] == 1


def test_it_says_out_loud_that_it_is_not_pass_at_k(files):
    ev, hist, sl = files
    _write(ev, hist, sl, {"a__a-1": True}, [], ["a__a-1"])
    r = report(str(ev), str(hist), str(sl))
    assert "not_pass_at_k" in r and "one graded attempt" in r["not_pass_at_k"]
