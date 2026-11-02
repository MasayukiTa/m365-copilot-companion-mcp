# -*- coding: utf-8 -*-
"""The SECOND class of provenance false positives: digits that belong to a standard, product,
or format NAME (ISO 8601, UTF-8, SHA-256, RFC <n>, HTTP 200, Python 3.10, CP932, base64), to a
unit of such a format, to an ordinal label ("第1段", "step 2", "attempt 1/3", "(1/3)"), to a
turn/job counter, a port, a time-of-day/timezone string, or to a code span -- none of which is
a measured analysis value.

Evidence: job e6541c75bcd9 (a Friday merge worker) ended VERIFY_FAILED on "8601" -- the "8601"
of the standard name "ISO 8601" in the worker's explanation of the judge_request format
("fetched_at(ISO 8601 の時刻)"). That is not a statistic anyone computed.

These drive relay/analysis_provenance.py directly -- pure functions over (answer text, this
run's computation outputs) -- so no ledger, clock, or worker is needed. Two guard tests at the
end prove the mask did NOT open a hole: a planted decimal and a planted count-with-a-unit that
appear in no computation output of this run are still rejected.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import analysis_provenance as ap  # noqa: E402


# ── the exact real false positive: "ISO 8601" → 8601 ──────────────────────────────────

def test_iso_8601_standard_name_is_not_a_claimed_number():
    # The shape that failed job e6541c75bcd9: the number 8601 came from the standard name
    # "ISO 8601" in prose explaining the judge_request format, not from any computation.
    vals = [v for v, _ in ap.extract_numbers(
        "必須キーは claim_id、data_location、fetched_at(ISO 8601 の時刻)。")]
    assert 8601 not in vals
    assert vals == []


def test_iso_8601_answer_with_a_real_computed_value_passes():
    # The only analysis value (0.42) was recomputed this run; the 8601 of "ISO 8601" must not
    # be treated as an unsupported claim.
    answer = "The correlation is 0.42; timestamps are ISO 8601 and the file is UTF-8."
    computed = ["recomputed correlation = 0.42 over the raw table"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert r["ok"], r
    assert 8601 not in r["unsupported"]


# ── the whole class of standard / product / format names and units ──────────────────────

def test_encoding_and_hash_names_are_not_claimed_numbers():
    answer = ("The export is UTF-8 (fallback UTF-16), checksummed with SHA-256 and SHA1, "
              "legacy files use CP932, the body is base64, hashed again with MD5.")
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (8, 16, 256, 1, 932, 64, 5):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_standard_reference_numbers_are_not_claimed():
    answer = "Timestamps follow RFC 3339, text per PEP 8, interfaces per IEEE 754."
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (3339, 8, 754):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_http_status_codes_are_not_claimed_numbers():
    answer = "The endpoint returned HTTP 200, then HTTP 404, over HTTP/1.1."
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (200, 404, 1):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_software_version_names_are_not_claimed_numbers():
    answer = ("Ran on Python 3.10 under Windows 11, read with Excel 2016 and openpyxl 3.1, "
              "via pyodbc 5.0 against SQL Server 2019.")
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (3.10, 11, 2016, 3.1, 5.0, 2019):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_percentile_names_are_not_claimed_numbers():
    # p95 / p99 are the NAMES of percentiles, not measured values of them.
    answer = "Latency p95 and p99 are reported; the p50 is the median."
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (95, 99, 50):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_ordinal_and_counter_labels_are_not_claimed_numbers():
    answer = ("第1段で抽出し、step 2 で集計、attempt 1/3 (2/3) で再試行、turn 10 job 5 "
              "phase 2 round 3 part 4 item 7 で完了。")
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (1, 2, 3, 10, 5, 4, 7):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_ports_and_timezones_are_not_claimed_numbers():
    answer = "Connected to DBB on port 50379 at :1433, time 2026-11-02T12:43:18+09:00 UTC+9."
    vals = [v for v, _ in ap.extract_numbers(answer)]
    for planted in (50379, 1433, 9):
        assert planted not in vals, (planted, vals)
    assert vals == []


def test_code_span_digits_are_not_claimed_numbers():
    answer = "Use `openpyxl 3.1` and the key `schema_v2` with `col_12` from `data_679.xlsx`."
    vals = [v for v, _ in ap.extract_numbers(answer)]
    assert vals == []


def test_a_standard_name_answer_with_a_real_value_still_passes():
    answer = ("Over 1,234 lots the defect share is 12.5%. Output is UTF-8, timestamps ISO 8601, "
              "computed on Python 3.10, see step 2 of 3 (HTTP 200).")
    computed = ["recomputed over 1,234 lots: defect share = 0.125"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert r["ok"], r
    # Only the two genuine statistics were policed, and both matched the computation.
    assert r["checked"] == 2


# ── GUARDS: the mask must not open a hole for a planted statistic ─────────────────────

def test_a_planted_decimal_next_to_a_standard_name_is_still_rejected():
    # A genuine statistic (0.834) copied from a results file, sitting right next to standard
    # names, must STILL fail: the class mask covers names, not bare decimals.
    answer = "The defect correlation is 0.834 (UTF-8 export, ISO 8601 timestamps)."
    computed = ["recomputed correlation = 0.207 over 74 lots"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert not r["ok"]
    assert any(abs(v - 0.834) < 1e-9 for v in r["unsupported"])
    assert 8601 not in r["unsupported"]
    assert 8 not in r["unsupported"]


def test_a_planted_count_with_a_unit_next_to_a_standard_name_is_still_rejected():
    # A count attached to a unit (3,500 lots) that appears in no computation output of this run
    # must STILL fail, even when it shares a sentence with ISO 8601 / Python 3.10.
    answer = ("We analysed 3,500 lots (timestamps ISO 8601, run on Python 3.10, SHA-256 "
              "verified).")
    computed = ["recomputed over 74 lots; mean = 12.3"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert not r["ok"]
    assert any(abs(v - 3500) < 1e-9 for v in r["unsupported"])
    # None of the standard-name digits leaked into the policed set.
    for name_digit in (8601, 3.10, 256):
        assert name_digit not in r["unsupported"], name_digit


def test_a_planted_percentage_next_to_a_version_name_is_still_rejected():
    answer = "Defect share 18.5% (computed with Python 3.10, Excel 2016, UTF-8)."
    computed = ["recomputed share = 0.207"]
    r = ap.check_answer_provenance(answer, computed, is_analysis=True)
    assert not r["ok"]
    assert any(abs(v - 18.5) < 1e-9 for v in r["unsupported"])
    for name_digit in (3.10, 2016, 8):
        assert name_digit not in r["unsupported"], name_digit
