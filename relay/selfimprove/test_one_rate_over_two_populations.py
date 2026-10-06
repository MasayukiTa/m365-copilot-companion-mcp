# -*- coding: utf-8 -*-
"""One completion rate over two populations whose base rates differ tenfold.

MEASURED 2026-09-12 over the live 267-row archive:

    ordinary goals    0.4529   (101/223)
    bench goals       0.0455   (2/44)
    what was reported 0.3858   -- the blend, and the only number a reader saw

`recent_completion_rate` was worse, because the recent window is the smallest and is therefore
the most completely taken over by whatever bulk job last ran. On 09-11/09-12 the last 50 rows
held 41 bench workers against 9 ordinary ones, so a published 0.20 was substantially a
SWE-bench resolve rate wearing the label "how the fleet is doing" -- while ordinary goals over
the same window completed at 0.8889.

I MISREAD THIS TWICE IN ONE SESSION before splitting it: first taking REFUSED at 29% for an
ongoing problem (it was one goal retried 71 times on a single day), then taking 87%
EVIDENCE_CONTRADICTED on 09-12 for a regression (all 20 were SWE-bench workers). An instrument
that misleads the person who just built it will mislead everyone.

THE CLASSIFIER IS THE INSTANCE ID. A harness goal names its instance, `sympy__sympy-12345`.
Checked against a Japanese-substring marker over the live archive, both selected exactly the
same 44 rows with no disagreement in either direction -- so the regex is validated rather than
assumed, and unlike the prose it does not stop working the day a harness prompt is in English.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import usage as U  # noqa: E402


def _row(goal="do a thing", status="done", outcome="DONE", seq=0, key=""):
    return {"goal": goal, "status": status, "outcome": outcome, "seq": seq, "key": key,
            "turn": 1}


def _archive(tmp_path, rows):
    p = tmp_path / "history.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return str(p)


BENCH_GOAL = "You are fixing a bug in sympy. Instance: sympy__sympy-12419"


# ── the classifier ────────────────────────────────────────────────────────────────────────

def test_a_harness_instance_is_recognised_by_its_id():
    assert U.is_bench_row(_row(goal=BENCH_GOAL))
    assert U.is_bench_row(_row(goal="", key="django__django-11532"))


def test_ordinary_work_is_not_mistaken_for_a_benchmark():
    for goal in ("今週のメールに返信案を作って",
                 "Summarise the meeting notes on my Desktop",
                 "Check whether the statue claim is true",
                 "release 2024-05 notes"):
        assert not U.is_bench_row(_row(goal=goal)), goal


def test_the_classifier_does_not_depend_on_the_prompt_language():
    """The first marker to hand was a Japanese substring from the current harness prompt. It
    agreed with the id regex on all 44 live rows -- and would stop working the day a harness
    prompt is written in English, silently folding bench rows back into the ordinary rate."""
    english = "You are fixing a real bug in the library pytest. Instance pytest-dev__pytest-5692"
    assert U.is_bench_row(_row(goal=english))


def test_a_malformed_row_is_not_a_benchmark():
    assert not U.is_bench_row(None)
    assert not U.is_bench_row("a string")
    assert not U.is_bench_row({})


# ── the split ─────────────────────────────────────────────────────────────────────────────

def test_each_population_gets_its_own_rate(tmp_path):
    rows = ([_row(seq=i) for i in range(8)]                                  # 8 ordinary, done
            + [_row(goal=BENCH_GOAL, status="stuck", outcome="STUCK", seq=100 + i)
               for i in range(12)])                                          # 12 bench, none done
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    w = out["workload"]
    assert w["ordinary"]["completion_rate"] == 1.0
    assert w["bench"]["completion_rate"] == 0.0
    assert w["ordinary"]["n"] == 8 and w["bench"]["n"] == 12


def test_every_row_lands_in_exactly_one_population(tmp_path):
    """A split whose parts do not add up would let rows vanish from both rates at once."""
    rows = [_row(seq=i) for i in range(5)] + [_row(goal=BENCH_GOAL, seq=50 + i) for i in range(7)]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    w = out["workload"]
    assert w["ordinary"]["n"] + w["bench"]["n"] == out["n_tasks"] == 12


def test_each_rate_carries_its_own_denominator(tmp_path):
    """THE REASON THE RECENT WINDOW MISLED. A rate over nine rows and a rate over forty-one
    must not be shown as the same kind of thing."""
    rows = [_row(seq=i) for i in range(3)] + [_row(goal=BENCH_GOAL, seq=9 + i) for i in range(40)]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    for arm in ("ordinary", "bench"):
        assert "n" in out["workload"][arm] and "recent_n" in out["workload"][arm]


def test_an_absent_population_reports_none_not_zero(tmp_path):
    """Zero completions and nothing to measure are different facts. A 0.0 beside an empty arm
    reads as a measured failure, which is the mistake verify_rate already made once."""
    rows = [_row(seq=i) for i in range(4)]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    assert out["workload"]["bench"]["n"] == 0
    assert out["workload"]["bench"]["completion_rate"] is None


# ── what must NOT change ──────────────────────────────────────────────────────────────────

def test_the_published_blended_rate_keeps_its_meaning(tmp_path):
    """Redefining a number in place makes a dashboard plot a different quantity against its own
    history without saying so -- the same defect facing the other way. The split is ADDED."""
    rows = [_row(seq=i) for i in range(8)] + [
        _row(goal=BENCH_GOAL, status="stuck", outcome="STUCK", seq=100 + i) for i in range(12)]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    assert out["completion_rate"] == round(8 / 20, 4)


def test_the_blended_trend_says_that_it_is_blended(tmp_path):
    """Each trend bucket is whatever workload was running then, so the series tracks the
    schedule. It is kept -- it is still the only per-period series here -- and labelled."""
    rows = [_row(seq=i) for i in range(12)]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    assert out.get("trend_is_blended") is True


def test_a_contradicted_completion_is_still_not_a_completion_in_either_arm(tmp_path):
    """The earlier correction (a DONE the record contradicts is not a completion) must hold
    inside each population, not just in the blend."""
    rows = [_row(seq=0), _row(seq=1, outcome="EVIDENCE_CONTRADICTED"),
            _row(goal=BENCH_GOAL, seq=2),
            _row(goal=BENCH_GOAL, seq=3, outcome="EVIDENCE_CONTRADICTED")]
    out = U.usage_section(history_path=_archive(tmp_path, rows),
                          status_path=str(tmp_path / "nope.json"))
    assert out["workload"]["ordinary"]["completion_rate"] == 0.5
    assert out["workload"]["bench"]["completion_rate"] == 0.5


# ── against the real archive, when it is here ─────────────────────────────────────────────

def test_the_live_archive_still_splits_into_two_populations():
    """Runs over the real .fleet/history.json when present. Skips in CI, where there is none --
    a question that cannot be asked must not be failed."""
    p = os.path.join(REPO, ".fleet", "history.json")
    if not os.path.isfile(p):
        pytest.skip("no live archive here")
    out = U.usage_section()
    w = out.get("workload") or {}
    assert w.get("ordinary", {}).get("n", 0) + w.get("bench", {}).get("n", 0) == out["n_tasks"]
