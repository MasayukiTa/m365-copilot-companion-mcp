# -*- coding: utf-8 -*-
"""The THIRD class of provenance false positives: digits that are part of an IDENTIFIER the
requester supplied -- the case NAME in their own words -- rather than a computed result.

Evidence: job jobsyn0001 (the "679#study" analysis, worker wNN, 2026-11-02) ended
VERIFY_FAILED "provenance: claimed numbers not found in this run's computation outputs: 679".
The ONLY unsupported number was 679, which is the NAME of the case: the request folder is
"sampleuser_679study", the submitter is "esyn_v01", the suggested claim_id is "sample_claim".
Every genuine statistic the answer stated (K=6, the per-lot rates, the bootstrap
correlations) was found in this run's own computation output. A name the requester wrote is not
a computed result, so it must not be policed as a claimed number.

THE RULE. check_answer_provenance now takes the requester's own words (request_text = goal +
resolved request text). A digit run that appears there is treated as an identifier -- NOT a
claim -- when, in the answer, it is glued to a letter / underscore / '#' / path separator / CJK
character (as in "679#study") rather than standing alone as a stated quantity. A QUANTITY form
("679 count", "679%", "n = 679") is still policed even when the same digits are in the goal: a
stated measurement must always be found in a computation output.

These drive relay/analysis_provenance.py directly -- pure functions over (answer text, this
run's computation outputs, request text) -- so no ledger, clock, or worker is needed. Guard
tests at the end prove the exemption did NOT open a hole: a planted "n = 679" stated as a
quantity with no computation still fails, and a non-request number next to CJK stays policed.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402


# The requester's own words for job jobsyn0001, copied only as far as the identifiers go
# (no employee ids, no credentials): the folder/case name, the submitter token, the claim_id.
REQUEST_TEXT = (
    "C:/Users/.../Downloads/sampleuser_679#study study this."
    "claim_id example sample_claim. submitter esyn_v01."
)

# A faithful slice of the real answer's shape (job jobsyn0001): it refers to the case by
# name ("679#study") and states statistics that WERE recomputed this run. Only the text needed
# for the assertion is reproduced; no employee ids or credentials.
REAL_ANSWER = (
    "679#study recompute result. Unique lots matched against trace data K=6. "
    "per-lot rate = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]. "
    "metric_R correlation r=+0.5, CI [0.4, 0.6]."
)

# The computation outputs of this run that back every genuine statistic above. (The CI level
# name "95" is deliberately NOT part of this fixture: whether a confidence LEVEL like 95% is a
# claimed statistic is a separate class of false positive, out of scope for this change, so the
# fixture states the interval by its bounds only to keep the test about the 679 case name.)
REAL_COMPUTED = [
    "K_unique_lots = 6",
    "per_lot_rate = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]",
    "boot corr metric_R r = 0.5 ci = [0.4, 0.6]",
]


# -- extraction: the case name is not a claimed number --------------------------------

def test_request_digit_runs_pulls_multi_digit_runs_only():
    runs = ap._request_digit_runs(REQUEST_TEXT)
    assert "679" in runs
    # single digits are not exempting tokens (they would exempt real 1-digit statistics)
    assert all(len(r) >= 2 for r in runs)


def test_case_name_679_glued_to_cjk_is_not_a_claim():
    # "679\u5024" -- the requester's case name with a generic CJK char glued to the right
    # (no business term); the glue is what the exemption keys on, not the word's meaning.
    rd = ap._request_digit_runs(REQUEST_TEXT)
    vals = [v for v, _ in ap.extract_numbers("679\u5024 recompute.", rd)]
    assert 679 not in vals


def test_case_name_in_identifier_tokens_is_not_a_claim():
    rd = ap._request_digit_runs(REQUEST_TEXT)
    for answer in ("claim_id is sample_claim.", "submitter esyn_v01 analysis.",
                   "folder sampleuser_679#study read.", "e679 input data."):
        vals = [v for v, _ in ap.extract_numbers(answer, rd)]
        assert 679 not in vals, answer


def test_without_request_text_the_old_behaviour_is_unchanged():
    # With no request_text, "679\u5024" still yields 679 -- the exemption is opt-in and does not
    # silently weaken the gate for callers that do not pass the requester's words.
    vals = [v for v, _ in ap.extract_numbers("679\u5024 recompute.")]
    assert 679 in vals


# -- the real rejected answer now PASSES except for genuinely unsupported numbers ----------

def test_real_679_answer_passes_when_statistics_are_recomputed():
    r = ap.check_answer_provenance(REAL_ANSWER, REAL_COMPUTED, is_analysis=True,
                                   request_text=REQUEST_TEXT)
    assert r["ok"], r
    assert 679 not in r["unsupported"]


def test_real_679_answer_without_request_text_still_flags_the_name():
    # Proof the fix is what rescues the real answer: WITHOUT the requester's words, the case
    # name 679 is (wrongly) policed and, absent from the computation, fails -- exactly the
    # false positive job jobsyn0001 hit.
    r = ap.check_answer_provenance(REAL_ANSWER, REAL_COMPUTED, is_analysis=True)
    assert not r["ok"]
    assert 679 in r["unsupported"]


# -- GUARDS: the exemption must not open a hole ----------------------------

def test_a_planted_quantity_n_equals_679_still_fails():
    # "n = 679" is a STATED quantity, not an identifier, even though 679 is in the goal. With no
    # computation producing a 679, it must still fail -- the quantity form is never exempted.
    answer = "The analyzed lot count was n = 679 items."
    computed = ["K_unique_lots = 6"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True,
                                   request_text=REQUEST_TEXT)
    assert not r["ok"]
    assert 679 in r["unsupported"]


def test_a_planted_percentage_679_still_fails():
    answer = "The rate was 679%."
    computed = ["recomputed share = 0.207"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True,
                                   request_text=REQUEST_TEXT)
    assert not r["ok"]
    assert 679 in r["unsupported"]


def test_a_non_request_number_next_to_cjk_stays_policed():
    # 20 is NOT in the requester's words, so "20\u500d" (a generic CJK fold suffix, not a business
    # term) is policed as before: a magnitude with no computation behind it still fails. The
    # exemption is scoped to requester-supplied digits.
    answer = "Median 20\u500d difference between units."
    computed = ["recomputed spearman rho = 0.208"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True,
                                   request_text=REQUEST_TEXT)
    assert not r["ok"]
    assert 20 in r["unsupported"]


def test_a_planted_decimal_in_the_679_answer_is_still_rejected():
    # A genuine statistic (0.321) that appears in no computation output must still fail, even
    # with the case name exempted in the same answer.
    answer = "679#study: correlation is 0.321."
    computed = ["recomputed correlation = 0.207 over 74 lots"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True,
                                   request_text=REQUEST_TEXT)
    assert not r["ok"]
    assert any(abs(v - 0.321) < 1e-9 for v in r["unsupported"])
    assert 679 not in r["unsupported"]
