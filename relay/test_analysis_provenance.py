# -*- coding: utf-8 -*-
"""The provenance gate: a number in a data-analysis answer must have been recomputed in this
run, not transcribed from a results file the folder already held.

These drive relay/analysis_provenance.py directly -- pure functions over (answer text, this
run's computation outputs) -- so no ledger, clock, or worker is needed. The hermetic
case-folder test that plants a fake results file lives in
test_a_planted_results_file_is_not_an_answer.py; this file pins the matching rules, including
the conservative allowances (rounding, units, percent-vs-fraction) that keep it from rejecting
honest answers.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402


# ── extraction ────────────────────────────────────────────────────────────────────────────

def test_extract_pulls_decimals_percents_and_thousands():
    vals = [v for v, _ in ap.extract_numbers(
        "correlation 0.42, n = 1,234, rate 12.5%, p=0.003")]
    assert 0.42 in vals and 1234.0 in vals and 12.5 in vals and 0.003 in vals


def test_extract_drops_years_so_a_date_is_not_a_claim():
    # A bare 2026 is a date, not a statistic; keeping it would force every answer that names a
    # year to 'justify' it against a computation.
    vals = [v for v, _ in ap.extract_numbers("run on 2026-11-02 over the table")]
    assert 2026.0 not in vals
    # ...but a decimal that merely falls in the year range is a real quantity and stays.
    vals2 = [v for v, _ in ap.extract_numbers("mean 2025.5 units")]
    assert 2025.5 in vals2


def test_extract_does_not_split_identifiers():
    # engine_v1_8 must not yield 1 and 8 as claimed numbers.
    vals = [v for v, _ in ap.extract_numbers("using engine_v1_8 config_12")]
    assert vals == []


# ── a non-analysis answer is never policed ────────────────────────────────────────────────

def test_non_analysis_answer_passes_untouched():
    r = ap.check_answer_provenance("I updated 3 files and fixed 2 bugs.", [],
                                   is_analysis=False)
    assert r["ok"] and r["reason"] == "not an analysis answer"


def test_analysis_answer_with_no_numbers_passes():
    r = ap.check_answer_provenance("The two groups did not differ meaningfully.", [],
                                   is_analysis=True)
    assert r["ok"] and r["checked"] == 0


# ── RULE 3: numbers but zero computation ──────────────────────────────────────────────────

def test_numbers_with_no_computation_are_rejected():
    r = ap.check_answer_provenance("The correlation is 0.87 (strong).", [],
                                   is_analysis=True)
    assert not r["ok"]
    assert not r["had_compute"]
    assert 0.87 in r["unsupported"]
    assert r["correction"]


# ── RULE 2: a planted number absent from this run's outputs is rejected ────────────────────

def test_a_number_only_in_a_results_file_is_rejected():
    # The computation this run actually ran produced 0.31; the answer reports 0.87, which
    # exists only in a results file the folder already held. That is the transcription.
    computed = ["recomputed correlation = 0.31 over 1,234 rows"]
    r = ap.check_answer_provenance("The correlation is 0.87.", computed, is_analysis=True)
    assert not r["ok"]
    assert 0.87 in r["unsupported"]
    assert 0.31 not in r["unsupported"]


def test_a_recomputed_number_passes():
    computed = ["correlation = 0.871 over the raw table"]
    r = ap.check_answer_provenance("We find a correlation of 0.87.", computed,
                                   is_analysis=True)
    assert r["ok"], r
    assert r["checked"] == 1


# ── conservative allowances: these MUST still pass (no false rejection) ────────────────────

def test_rounding_difference_still_matches():
    computed = ["mean = 1234.4987"]
    r = ap.check_answer_provenance("The mean is 1234.5.", computed, is_analysis=True)
    assert r["ok"], r


def test_percent_written_against_a_computed_fraction_matches():
    # The run computed a fraction 0.125; the answer reported it as 12.5%.
    computed = ["share = 0.125"]
    r = ap.check_answer_provenance("The defect share is 12.5%.", computed, is_analysis=True)
    assert r["ok"], r


def test_fraction_written_against_a_computed_percent_matches():
    computed = ["rate 12.5 percent"]
    r = ap.check_answer_provenance("The rate is 0.125.", computed, is_analysis=True)
    assert r["ok"], r


def test_a_correlations_sign_is_a_direction_not_a_mismatch():
    computed = ["pearson r = -0.42"]
    r = ap.check_answer_provenance("A correlation of 0.42 was observed.", computed,
                                   is_analysis=True)
    assert r["ok"], r


def test_a_tiny_pvalue_matches_within_the_absolute_floor():
    # A p-value reported as 0.000 against a computed 0.0004: both are below ABS_FLOOR and read
    # as the same near-zero quantity, so a reader who rounded to three places is not rejected.
    computed = ["p = 0.0004"]
    r = ap.check_answer_provenance("p is 0.000 (below threshold).", computed,
                                   is_analysis=True)
    assert r["ok"], r


def test_a_pvalue_just_outside_the_floor_is_not_waved_through():
    # 0.001 vs 0.0004 differ by 6e-4, past the absolute floor: the gate does NOT treat them as
    # the same number, so the floor is a floor and not a blanket pass for anything small.
    computed = ["p = 0.0004"]
    r = ap.check_answer_provenance("p is 0.001.", computed, is_analysis=True)
    assert not r["ok"]
    assert 0.001 in r["unsupported"]


def test_a_large_count_is_not_matched_by_a_far_off_one():
    # 5000 is outside the year range so it is a real claimed count; it must not be waved
    # through by a computed 1000 that is nowhere near it.
    computed = ["n = 1000"]
    r = ap.check_answer_provenance("The sample size was 5,000.", computed, is_analysis=True)
    assert not r["ok"]
    assert 5000.0 in r["unsupported"]


# ── the note that carries provenance into the judge input (owner rule 4) ───────────────────

def test_note_states_pass_with_a_count():
    r = ap.check_answer_provenance("corr 0.5", ["corr = 0.5"], is_analysis=True)
    assert "provenance: ok" in ap.provenance_note(r)


def test_note_states_fail_with_the_unsupported_number():
    r = ap.check_answer_provenance("corr 0.9", ["corr = 0.1"], is_analysis=True)
    note = ap.provenance_note(r)
    assert "provenance: FAIL" in note and "0.9" in note


def test_note_states_no_computation_case():
    r = ap.check_answer_provenance("corr 0.9", [], is_analysis=True)
    assert "no computation" in ap.provenance_note(r)


# ── FALSE POSITIVES from two real rejected answers (PR fix) ────────────────────────────────
# Both shapes below were taken from real jobs the gate wrongly failed:
#   * job 8c3daa76ccf0 failed on 511 -- the leading digits of the citation tag
#     [ref:511e078026f6] in the worker's answer, not an analysis value.
#   * job 3da486070941 failed on 20 -- a version/id token in the answer, not a statistic.
# These are hermetic: pure strings, no ledger/clock/IO.

def test_citation_ref_tag_is_not_a_claimed_number():
    # [ref:511e078026f6] must not yield 511 (nor any hash digit) as a claimed number.
    vals = [v for v, _ in ap.extract_numbers(
        "The lot-level kasure rate is summarised below [ref:511e078026f6].")]
    assert 511 not in vals
    assert vals == []


def test_citation_ref_tag_answer_with_a_real_computed_value_passes():
    # The only analysis value (0.12) was recomputed this run; the 511 of the [ref:...] tag
    # must not be treated as an unsupported claim.
    answer = "The kasure rate is 0.12 over the raw lots [ref:511e078026f6]."
    computed = ["recomputed kasure rate = 0.12 over 74 lots"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert r["ok"], r
    assert 511 not in r["unsupported"]


def test_version_token_v20_is_not_a_claimed_number():
    # The shape that produced the spurious 20: a version/id token in a screening answer.
    for answer in ("Results written to screening_v20 folder.",
                   "See the _v20 output; defect totals are listed there.",
                   "Analysis version 20 of the screening."):
        vals = [v for v, _ in ap.extract_numbers(answer)]
        assert 20 not in vals, answer


def test_hex_ids_dates_paths_and_pr_numbers_are_not_claimed_numbers():
    answer = ("Job 8c3daa76ccf0 ran on 2026-11-02 at 12:43, see "
              "C:/out/calc_top50_rows.csv, merged in #401, commit 267a5740.")
    vals = [v for v, _ in ap.extract_numbers(answer)]
    # None of these id/metadata digits should become a policed claim.
    for planted in (8, 50, 401, 267, 2026, 11, 12, 43):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_markdown_list_numbering_is_not_a_claimed_number():
    answer = "1. first finding\n2) second finding\n10. tenth finding"
    vals = [v for v, _ in ap.extract_numbers(answer)]
    assert vals == []


def test_a_planted_decimal_copied_from_a_result_file_is_still_rejected():
    # Guard against opening a hole: a genuine statistic (a decimal) that appears ONLY in a
    # results file and in no computation output of this run must still fail. The masking of
    # ids/tags must not swallow a bare decimal next to a citation.
    answer = "The defect correlation is 0.834 [ref:511e078026f6]."
    computed = ["recomputed correlation = 0.207 over 74 lots"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert not r["ok"]
    assert any(abs(v - 0.834) < 1e-9 for v in r["unsupported"])
    assert 511 not in r["unsupported"]


def test_a_planted_percentage_next_to_metadata_is_still_rejected():
    answer = "Defect share 12.5% in run 8c3daa76ccf0 on 2026-11-02."
    computed = ["recomputed share = 0.207"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert not r["ok"]
    assert any(abs(v - 12.5) < 1e-9 for v in r["unsupported"])
