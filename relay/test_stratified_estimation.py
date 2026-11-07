# -*- coding: utf-8 -*-
"""Tests for relay.stratified_estimate -- the default WITHIN-stratum analysis used once a
comparison's design has been judged to hold.

These tests assert the three things the owner's objection makes load-bearing:
  * a shared stratum with enough lots in BOTH groups is pooled, and the pooled contrast lies
    between the per-stratum contrasts (it is a weighted average, not a pooled-confounded number);
  * a stratum with fewer than k_min lots in either group is reported with reason='below_k_min'
    and does NOT enter the pooled estimate -- k_min is honoured, never lowered;
  * the per-stratum TABLE is returned for the judge footer.
They are pure-Python and portable (no Windows-only paths), so the hermetic Linux CI collects and
runs them.
"""
from __future__ import annotations

import pytest

from relay.stratified_estimate import (
    DEFAULT_K_MIN,
    stratified_estimate,
    stratified_table,
)


def _synth_two_strata(k=8, delta_p=2.0, delta_q=4.0):
    """Two products P and Q, each with k lots per group. Group A baseline 10, group B raised by
    delta within each product. Returns equal-length y/x/strata/lot."""
    y, x, strata, lot = [], [], [], []
    for i in range(k):
        # product P
        y.append(10.0); x.append("A"); strata.append("P"); lot.append("P-A-%d" % i)
        y.append(10.0 + delta_p); x.append("B"); strata.append("P"); lot.append("P-B-%d" % i)
        # product Q
        y.append(20.0); x.append("A"); strata.append("Q"); lot.append("Q-A-%d" % i)
        y.append(20.0 + delta_q); x.append("B"); strata.append("Q"); lot.append("Q-B-%d" % i)
    return y, x, strata, lot


def test_shared_strata_are_pooled_within_product():
    y, x, strata, lot = _synth_two_strata(k=8, delta_p=2.0, delta_q=4.0)
    est = stratified_estimate(y, x, strata, lot)
    assert est["group_a"] == "A" and est["group_b"] == "B"
    assert est["n_strata_total"] == 2
    assert est["n_strata_pooled"] == 2
    # Each stratum's own contrast is exactly its delta (no noise in the synthetic data).
    per = {s["stratum"]: s["diff"] for s in est["strata"]}
    assert per["P"] == pytest.approx(2.0)
    assert per["Q"] == pytest.approx(4.0)
    # The pooled contrast is a weighted average, so it lies between the two per-stratum contrasts.
    assert 2.0 <= est["pooled_diff"] <= 4.0


def test_pooled_is_not_the_naive_cross_product_difference():
    # Make the groups unbalanced ACROSS products so a naive pooled mean(B)-mean(A) would be
    # distorted by product mix, but the stratified estimate stays product-fixed.
    y, x, strata, lot = [], [], [], []
    for i in range(10):
        y.append(10.0); x.append("A"); strata.append("P"); lot.append("P-A-%d" % i)
        y.append(12.0); x.append("B"); strata.append("P"); lot.append("P-B-%d" % i)
    for i in range(10):
        y.append(50.0); x.append("A"); strata.append("Q"); lot.append("Q-A-%d" % i)
        y.append(52.0); x.append("B"); strata.append("Q"); lot.append("Q-B-%d" % i)
    est = stratified_estimate(y, x, strata, lot)
    # Within each product the true effect is +2. The stratified pooled estimate recovers it.
    assert est["pooled_diff"] == pytest.approx(2.0, abs=1e-9)


def test_stratum_below_k_min_is_excluded_with_reason():
    # Product P has enough lots; product R has only 3 lots per group (< k_min default 8).
    y, x, strata, lot = _synth_two_strata(k=8, delta_p=2.0, delta_q=4.0)
    for i in range(3):
        y.append(5.0); x.append("A"); strata.append("R"); lot.append("R-A-%d" % i)
        y.append(9.0); x.append("B"); strata.append("R"); lot.append("R-B-%d" % i)
    est = stratified_estimate(y, x, strata, lot)
    by = {s["stratum"]: s for s in est["strata"]}
    assert by["R"]["eligible"] is False
    assert by["R"]["reason"] == "below_k_min"
    assert by["P"]["eligible"] is True
    assert by["Q"]["eligible"] is True
    # R is kept in the table but does not enter pooling.
    assert est["n_strata_total"] == 3
    assert est["n_strata_pooled"] == 2


def test_k_min_is_not_lowered_and_is_reported():
    y, x, strata, lot = _synth_two_strata(k=8)
    est = stratified_estimate(y, x, strata, lot)
    assert est["k_min"] == DEFAULT_K_MIN == 8
    # Raising k_min (stricter) is allowed and removes strata that no longer qualify.
    stricter = stratified_estimate(y, x, strata, lot, k_min=9)
    assert stricter["n_strata_pooled"] == 0
    assert all(s["reason"] == "below_k_min" for s in stricter["strata"])


def test_table_has_one_row_per_stratum_for_the_footer():
    y, x, strata, lot = _synth_two_strata(k=8)
    est = stratified_estimate(y, x, strata, lot)
    rows = stratified_table(est)
    assert len(rows) == est["n_strata_total"] == 2
    for r in rows:
        assert set(r.keys()) == {
            "stratum", "lots_a", "lots_b", "mean_a", "mean_b",
            "diff", "weight", "eligible", "reason",
        }
    strata_seen = {r["stratum"] for r in rows}
    assert strata_seen == {"P", "Q"}


def test_unequal_length_inputs_raise():
    with pytest.raises(ValueError):
        stratified_estimate([1.0, 2.0], ["A", "B"], ["P"], ["l1", "l2"])


def test_requires_exactly_two_groups():
    y = [1.0, 2.0, 3.0]
    x = ["A", "B", "C"]
    strata = ["P", "P", "P"]
    lot = ["a", "b", "c"]
    with pytest.raises(ValueError):
        stratified_estimate(y, x, strata, lot)


def test_nothing_eligible_yields_none_pooled():
    # Only 2 lots per group -> below k_min -> pooled estimate is None, not a fabricated number.
    y, x, strata, lot = [], [], [], []
    for i in range(2):
        y.append(1.0); x.append("A"); strata.append("P"); lot.append("P-A-%d" % i)
        y.append(3.0); x.append("B"); strata.append("P"); lot.append("P-B-%d" % i)
    est = stratified_estimate(y, x, strata, lot)
    assert est["pooled_diff"] is None
    assert est["pooled_se"] is None
    assert est["n_strata_pooled"] == 0
