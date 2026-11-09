# -*- coding: utf-8 -*-
"""Conservative-against-false-rejection, pinned end to end.

The provenance gate's whole value depends on NOT rejecting honest answers: a gate that fails
a correct, recomputed number because the writer rounded it, wrote it as a percent instead of a
fraction, or scaled its units, would be turned off -- and then it protects nothing. The owner
rule names exactly these three forms (rounding, units, percent-versus-fraction) as things that
must still match.

This is a hermetic case folder of the same shape as the planted-file test: a RAW input table,
and a PLANTED fake number that the raw data does not support. The recomputed correlation from
the raw table is reported in three honest surface forms; each must PASS against the run's own
compute output. The planted number, which no computation produced, must still FAIL -- so
'tolerant' never degrades into 'accepts anything'.

Stdlib only; no network, no DB, no pandas, no openpyxl. The raw table is built in-process and
the 'compute output' stands in for this run's run_python stdout, so the test is fully
hermetic and Linux-clean.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402


# A raw relationship whose Pearson r is a clean fraction well inside (0, 1): deterministic, so
# the recomputed number is fixed and its percent/rounded forms are known exactly.
_RAW_X = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
_RAW_Y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 11.5]

# The attacker's number: unrelated to the raw data, chosen so it cannot collide with the true
# correlation or any rounding of it.
_PLANTED = 0.123


def _pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / ((sxx * syy) ** 0.5)


def _recomputed():
    r = _pearson(_RAW_X, _RAW_Y)
    # The compute output the run's ledger would have captured from a run_python call.
    compute_output = "pearson r = %.6f over %d rows" % (r, len(_RAW_X))
    return r, compute_output


def test_a_percent_form_of_the_recomputed_fraction_passes():
    r, compute_output = _recomputed()
    assert 0.90 < r < 0.999  # a strong positive fraction, not 1.0
    # The writer reports the SAME recomputed value as a percent (fraction 0.97 -> "97%").
    answer = "Recomputing from the raw table, the correlation is about %.0f%%." % (r * 100.0)
    res = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert res["ok"], res
    assert not res["unsupported"], res


def test_a_rounded_form_of_the_recomputed_fraction_passes():
    r, compute_output = _recomputed()
    # Reported rounded to two decimals -- display precision, not a different number.
    answer = "The Pearson correlation computed in this run is %.2f." % r
    res = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert res["ok"], res
    assert not res["unsupported"], res


def test_the_same_number_with_its_sign_dropped_still_passes():
    # The gate does not insist on a sign it can recover from context; an answer that states the
    # magnitude of the recomputed value must not be rejected for omitting the plus sign.
    r, compute_output = _recomputed()
    answer = "Strength of association (|r|) computed here: %.3f." % abs(r)
    res = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert res["ok"], res


def test_a_planted_number_no_computation_produced_still_fails():
    # Same tolerant gate, a number the raw data never produced and no compute output contains.
    _r, compute_output = _recomputed()
    answer = "The correlation is %.3f." % _PLANTED
    res = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert not res["ok"], "a planted number with no computation behind it must be rejected"
    assert _PLANTED in res["unsupported"]
    assert res["correction"]


def test_tolerance_does_not_swallow_a_nearby_but_distinct_planted_number():
    # Guard the guard: a planted value close to the true one but outside REL_TOL must still
    # fail, so 'conservative against false rejection' is not an excuse to accept near misses.
    r, compute_output = _recomputed()
    distinct = round(r - 0.05, 3)  # far enough outside REL_TOL (5e-3) to be a different claim
    assert abs(distinct - r) > 0.01
    answer = "The correlation is %.3f." % distinct
    res = ap.check_answer_provenance(answer, [compute_output], is_analysis=True)
    assert not res["ok"], res
    assert distinct in res["unsupported"]
