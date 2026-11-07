# -*- coding: utf-8 -*-
"""stratified_estimate -- the DEFAULT analysis once a comparison's design has been judged to
hold (i.e. both groups share strata with enough lots each).

WHY THIS MODULE EXISTS.
  The owner's standing objection (2026-11-03..04) is that comparing two exposure groups whose
  PRODUCTS (塗工布品名) differ cannot separate an equipment effect from a product effect. The
  design-check seam (confound_gate / judge footer, implemented elsewhere) BLOCKS such a
  comparison before it is run. This module is the other half: when the design DOES hold, the
  comparison must still be estimated WITHIN each shared stratum and then pooled, never across
  pooled-and-confounded rows. That is stratified (Mantel-Haenszel-style) estimation.

WHAT THIS MODULE IS, AND IS NOT.
  * It computes a per-stratum point estimate of the exposure contrast and a precision-weighted
    pooled estimate, and it returns the per-stratum table so the judge footer can show it.
  * It does NOT touch the frozen validity engine, its delta_min, its k_min, or its correction.
    k_min is accepted as a parameter purely to DECIDE WHICH STRATA ARE ELIGIBLE for pooling
    (a stratum needs >= k_min lots in EACH group); it is never lowered here and never used to
    relax any verdict. Strata below k_min are reported with a reason, not silently dropped.
  * It never reads the design decision for the caller; a caller that has not had the design
    judged to hold should not call this. (The ineligible-strata accounting is informational.)

INPUTS are four EQUAL-LENGTH sequences, one entry per lot-level observation:
  y       response value for the lot (rate, count, or mean -- the analyst supplies the column)
  x       exposure group label for the lot (exactly two distinct values define the contrast)
  strata  stratum label for the lot (the shared product / line / period that must be held fixed)
  lot     lot identifier (used only to count lots per (stratum, group); need not be unique
          across strata)

The module reads y ONLY to estimate; the DESIGN judgement upstream reads no y at all. Keeping
estimation here and design-check elsewhere is what lets the design verdict be proven independent
of the outcome.
"""
from __future__ import annotations

from collections import defaultdict

#: Default minimum lots PER GROUP WITHIN A STRATUM for that stratum to be pooled. This mirrors
#: the engine's k_min (8) by owner instruction -- it is NOT the engine's threshold and does not
#: change it; it only decides stratum eligibility here. Do not lower it.
DEFAULT_K_MIN = 8

__all__ = [
    "DEFAULT_K_MIN",
    "StratumResult",
    "stratified_estimate",
    "stratified_table",
]


class StratumResult(dict):
    """One stratum's accounting. A dict subclass so it is trivially JSON-serialisable and can be
    dropped straight into the judge footer, while still having named access in code."""

    @property
    def stratum(self):
        return self.get("stratum")

    @property
    def eligible(self):
        return bool(self.get("eligible"))


def _two_groups(x_values):
    """Return the two group labels in a stable order, or raise if not exactly two."""
    seen = []
    for v in x_values:
        if v not in seen:
            seen.append(v)
    if len(seen) != 2:
        raise ValueError(
            "stratified_estimate expects exactly two exposure groups, got %d: %r"
            % (len(seen), seen)
        )
    return seen[0], seen[1]


def _mean(vals):
    return sum(vals) / len(vals)


def _var(vals):
    """Sample variance (n-1). Returns 0.0 when n < 2 (no within-group spread estimable)."""
    n = len(vals)
    if n < 2:
        return 0.0
    m = _mean(vals)
    return sum((v - m) ** 2 for v in vals) / (n - 1)


def stratified_estimate(y, x, strata, lot, *, k_min=DEFAULT_K_MIN):
    """Estimate the exposure contrast within each stratum and pool the eligible strata.

    Returns a dict (JSON-serialisable) with:
      group_a, group_b     the two exposure labels (contrast is mean(b) - mean(a))
      k_min                the eligibility threshold used (unchanged, informational)
      strata               list[StratumResult], ONE PER stratum (eligible and not), each with
                           stratum, n_a, n_b, lots_a, lots_b, mean_a, mean_b, diff, var, weight,
                           eligible, reason
      n_strata_total       number of distinct strata seen
      n_strata_pooled      number of eligible strata that entered the pooled estimate
      pooled_diff          precision-weighted pooled contrast, or None when nothing is eligible
      pooled_se            standard error of pooled_diff, or None
      pooled_weight_total  sum of weights used

    k_min and any engine threshold are NOT modified. Strata with fewer than k_min lots in EITHER
    group are marked eligible=False with reason='below_k_min' and excluded from pooling, but kept
    in the table.
    """
    y = list(y)
    x = list(x)
    strata = list(strata)
    lot = list(lot)
    n = len(y)
    if not (len(x) == len(strata) == len(lot) == n):
        raise ValueError(
            "stratified_estimate requires equal-length y/x/strata/lot; got "
            "%d/%d/%d/%d" % (n, len(x), len(strata), len(lot))
        )
    if n == 0:
        raise ValueError("stratified_estimate requires at least one observation")

    group_a, group_b = _two_groups(x)

    # Bucket values and lot ids per (stratum, group).
    vals = defaultdict(lambda: {group_a: [], group_b: []})
    lots = defaultdict(lambda: {group_a: set(), group_b: set()})
    order = []
    for yi, xi, si, li in zip(y, x, strata, lot):
        if si not in vals:
            order.append(si)
        vals[si][xi].append(float(yi))
        lots[si][xi].add(li)

    results = []
    num = 0.0        # sum of weight * diff
    den = 0.0        # sum of weight
    var_num = 0.0    # sum of weight^2 * var(diff), for SE
    pooled_count = 0

    for si in order:
        va = vals[si][group_a]
        vb = vals[si][group_b]
        lots_a = len(lots[si][group_a])
        lots_b = len(lots[si][group_b])
        n_a = len(va)
        n_b = len(vb)
        mean_a = _mean(va) if va else None
        mean_b = _mean(vb) if vb else None
        diff = (mean_b - mean_a) if (va and vb) else None

        # Variance of the stratum difference of means (independent groups).
        if n_a >= 1 and n_b >= 1:
            var_diff = _var(va) / n_a + _var(vb) / n_b
        else:
            var_diff = None

        if lots_a < k_min or lots_b < k_min:
            eligible = False
            reason = "below_k_min"
            weight = 0.0
        elif diff is None:
            eligible = False
            reason = "group_absent"
            weight = 0.0
        else:
            eligible = True
            reason = ""
            # Inverse-variance weight; when variance is 0 (degenerate), fall back to total lots
            # as the weight so a perfectly-consistent stratum still contributes.
            if var_diff and var_diff > 0:
                weight = 1.0 / var_diff
            else:
                weight = float(lots_a + lots_b)

        if eligible:
            num += weight * diff
            den += weight
            if var_diff and var_diff > 0:
                var_num += (weight ** 2) * var_diff
            pooled_count += 1

        results.append(StratumResult(
            stratum=si,
            n_a=n_a,
            n_b=n_b,
            lots_a=lots_a,
            lots_b=lots_b,
            mean_a=mean_a,
            mean_b=mean_b,
            diff=diff,
            var=var_diff,
            weight=weight,
            eligible=eligible,
            reason=reason,
        ))

    if den > 0:
        pooled_diff = num / den
        # SE of inverse-variance pooled estimate is 1/sqrt(sum weights) when weights are 1/var.
        # When any fallback weight was used, report the conservative sqrt(var_num)/den instead.
        pooled_se = (1.0 / den) ** 0.5 if var_num == 0.0 else (var_num ** 0.5) / den
    else:
        pooled_diff = None
        pooled_se = None

    return {
        "group_a": group_a,
        "group_b": group_b,
        "k_min": k_min,
        "strata": results,
        "n_strata_total": len(order),
        "n_strata_pooled": pooled_count,
        "pooled_diff": pooled_diff,
        "pooled_se": pooled_se,
        "pooled_weight_total": den,
    }


def stratified_table(estimate):
    """Render a stratified_estimate() result as a list of flat rows for the judge footer.

    Returns list[dict] with stable keys: stratum, lots_a, lots_b, mean_a, mean_b, diff, weight,
    eligible, reason. The pooled summary is NOT a row; read it from the estimate dict.
    """
    rows = []
    for s in estimate.get("strata", []):
        rows.append({
            "stratum": s.get("stratum"),
            "lots_a": s.get("lots_a"),
            "lots_b": s.get("lots_b"),
            "mean_a": s.get("mean_a"),
            "mean_b": s.get("mean_b"),
            "diff": s.get("diff"),
            "weight": s.get("weight"),
            "eligible": s.get("eligible"),
            "reason": s.get("reason"),
        })
    return rows
