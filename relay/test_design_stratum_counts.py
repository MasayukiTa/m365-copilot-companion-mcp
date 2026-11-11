# -*- coding: utf-8 -*-
"""Tests for relay.design_stratum_counts and its use by relay.judge_claim_preflight.

The design preflight must answer whether two exposure groups can be compared WITHIN a shared
stratum, and it must do so WITHOUT ever reading the response column -- the design verdict has to
be provably independent of the outcome. These tests assert exactly that:

  * the response column is NEVER read: the synthetic CSV's response column holds a value that
    would raise if it were ever converted to a number, and counting still succeeds;
  * counts are per exposure GROUP and per STRATUM, measured as distinct lot ids;
  * a stratum shared by both groups with >= K_MIN_LOTS lots in each is counted as meeting the
    minimum, while one short in either group is not;
  * judge_claim_preflight now ADDS a design problem for a claim whose data has no shared
    stratum meeting the minimum, and adds NONE when a qualifying shared stratum exists.

Pure-Python and portable (no Windows-only paths), so the hermetic Linux CI collects and runs it.
"""
from __future__ import annotations

import os

from relay.design_stratum_counts import (
    K_MIN_LOTS,
    build_stratum_counts,
    shared_stratum_summary,
)
from relay import judge_claim_preflight


#: A response value that is fine as text but raises if anything tries to float() it. If the
#: design counting ever parsed the response column, the test that uses this CSV would fail.
_POISON_RESPONSE = "not-a-number-do-not-parse"


def _claim(stratifiers=("stratum",), exposure="group", unit="unit"):
    return {
        "stratifiers": list(stratifiers),
        "exposure": {"column": exposure},
        "hierarchy": {"unit_column": unit},
        "response": {"column": "response"},
    }


def _write_csv(tmp_path, rows, header=("unit", "group", "stratum", "response")):
    """Write a CSV whose response column holds a non-numeric poison value on every row."""
    path = os.path.join(str(tmp_path), "data.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            f.write(",".join(row) + "\n")
    return path


def _balanced_rows(strata_lots):
    """Build rows for {stratum: (lots_in_A, lots_in_B)} with a poison response on each row."""
    rows = []
    for stratum, (n_a, n_b) in strata_lots.items():
        for i in range(n_a):
            rows.append(("%s-A-%d" % (stratum, i), "A", stratum, _POISON_RESPONSE))
        for i in range(n_b):
            rows.append(("%s-B-%d" % (stratum, i), "B", stratum, _POISON_RESPONSE))
    return rows


def test_constant_is_eight():
    assert K_MIN_LOTS == 8


def test_response_column_is_never_read(tmp_path):
    # Enough lots in both groups of a shared stratum, so counting does real work; the response
    # column is poison, so any read of it would raise. Counting must still succeed.
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 8)}))
    result = build_stratum_counts(_claim(), path)
    assert result["response_column_read"] is False
    assert result["stratifier_columns"] == ["stratum"]
    # The one shared stratum P has 8 lots in each group.
    assert result["counts"]["stratum"]["A"]["P"] == 8
    assert result["counts"]["stratum"]["B"]["P"] == 8


def test_counts_are_per_group_and_per_stratum(tmp_path):
    # P: 8 A-lots / 5 B-lots; Q: 3 A-lots / 9 B-lots. Distinct lot ids per cell.
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 5), "Q": (3, 9)}))
    counts = build_stratum_counts(_claim(), path)["counts"]["stratum"]
    assert counts["A"] == {"P": 8, "Q": 3}
    assert counts["B"] == {"P": 5, "Q": 9}


def test_distinct_lots_are_counted_not_rows(tmp_path):
    # The same lot id appearing on several rows counts once (distinct units, not rows).
    rows = [
        ("L1", "A", "P", _POISON_RESPONSE),
        ("L1", "A", "P", _POISON_RESPONSE),
        ("L2", "A", "P", _POISON_RESPONSE),
        ("L3", "B", "P", _POISON_RESPONSE),
    ]
    path = _write_csv(tmp_path, rows)
    counts = build_stratum_counts(_claim(), path)["counts"]["stratum"]
    assert counts["A"]["P"] == 2
    assert counts["B"]["P"] == 1


def test_shared_stratum_meeting_k_min_is_counted(tmp_path):
    # P is shared with >= 8 lots in each group (qualifies); Q is shared but short in group B;
    # R exists only in group A (not shared at all).
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 10), "Q": (8, 3), "R": (8, 0)}))
    counts = build_stratum_counts(_claim(), path)["counts"]["stratum"]
    summary = shared_stratum_summary(counts, k_min=K_MIN_LOTS)
    assert summary["k_min"] == K_MIN_LOTS
    # P and Q appear in both groups; R does not.
    assert summary["shared_strata"] == 2
    # Only P has >= 8 lots in BOTH groups.
    assert summary["shared_strata_meeting_k_min"] == 1


def test_summary_with_fewer_than_two_groups_is_empty():
    assert shared_stratum_summary({}, k_min=K_MIN_LOTS) == {
        "k_min": K_MIN_LOTS,
        "shared_strata": 0,
        "shared_strata_meeting_k_min": 0,
    }
    one_group = {"A": {"P": 9}}
    assert shared_stratum_summary(one_group)["shared_strata"] == 0


def test_no_stratifier_column_yields_no_counts(tmp_path):
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 8)}))
    claim = _claim(stratifiers=())  # no stratifier declared
    result = build_stratum_counts(claim, path)
    assert result["stratifier_columns"] == []
    assert result["counts"] == {}


def test_preflight_adds_design_problem_when_no_shared_stratum_meets_k_min(tmp_path):
    # Q is shared but short in B; no stratum is shared with >= 8 lots in each group.
    path = _write_csv(tmp_path, _balanced_rows({"Q": (8, 3), "R": (8, 0)}))
    problems = judge_claim_preflight._design_problems(_claim(), path)
    assert any("design:" in p and "within-stratum contrast" in p for p in problems)


def test_preflight_reports_missing_stratifier(tmp_path):
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 8)}))
    problems = judge_claim_preflight._design_problems(_claim(stratifiers=()), path)
    assert any("no stratifier column" in p for p in problems)


def test_preflight_adds_no_design_problem_when_a_shared_stratum_qualifies(tmp_path):
    # P is shared with 8 lots in each group -> design can carry a within-stratum contrast.
    path = _write_csv(tmp_path, _balanced_rows({"P": (8, 8)}))
    problems = judge_claim_preflight._design_problems(_claim(), path)
    assert problems == []
