# -*- coding: utf-8 -*-
"""The rule given one home so it could not be copied again had been copied ten more times.

`bench/verdicts.py`'s module docstring is an incident report, and it ends with a promise:

    "This row is not a measurement" was written five times, as a literal comparison against
    the string EVALERR, in five files that each had to be found and changed together. They
    were not. Adding NOPATCH to the vocabulary broke four of the five silently... pro_cycle
    .graded_ids() correctly left a NOPATCH instance outstanding so the cycle would run it
    again, while pro_grade_remote.ingest() counted that same row as a verdict already held and
    DISCARDED the real one when it arrived. The instance would have been re-run for ever and
    never recorded.
    ... A sixth copy would have been the same mistake again, so the rule now has one home and
    the copies import it.

Half of that happened. `is_measurement()` was adopted; `is_resolved()` was not, and sat in this
repository's unreached baseline while ten literal `== "RESOLVED"` comparisons lived in seven
files -- two of them on the line below a call to `is_measurement`.

THEY WERE WRONG, NOT MERELY DUPLICATED. `test_a_boolean_verdict_is_a_grade` below is the
difference: `normalise()` turns a boolean into a verdict string because "Some producers write
{instance_id: bool}", and a raw comparison reads `True` as NOT resolved. Every one of those
sites under-counted the resolve rate for such a producer -- the same silent miscount the
module was written after.

`test_no_file_compares_a_verdict_to_a_literal` is the part that keeps the promise: it fails on
the eleventh copy.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench import verdicts as V  # noqa: E402

#: A literal comparison against a verdict word, in either direction. The vocabulary is small
#: and closed (verdicts.py names it), so the pattern can name it too rather than guessing.
_LITERAL = re.compile(r"""[!=]=\s*["'](RESOLVED|EVALERR|NOPATCH)["']""")

#: RESOLVED is the measurement decision and belongs to `is_resolved` alone.
_MEASUREMENT_WORD = "RESOLVED"


def _code_only(line):
    """The line with any trailing comment removed.

    A COMMENT QUOTING THE OLD CODE IS DOCUMENTATION, and forbidding it would push people to
    delete the explanation of why the rule exists. `bench/pro_grade_remote.py:323` is exactly
    that: a comment reading 'This read `!= "EVALERR"`, so the moment...' -- the incident report
    for this very rule, flagged by the first version of this check as an offence.

    Crude on purpose: a `#` inside a string literal would truncate the line early, which can
    only cause a missed detection on a line that also contains a comparison, and every such
    line in this tree is caught by the import check below.
    """
    return line.split("#", 1)[0]

#: verdicts.py IS the definition; a test that exercises the vocabulary has to name the words.
_ALLOWED = {"verdicts.py", "test_resolved_has_one_home.py"}


def _bench_sources():
    root = os.path.join(REPO, "bench")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".pytest_cache"}]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


# ── why the copies were wrong, not merely redundant ───────────────────────────────────────

def test_a_boolean_verdict_is_a_grade():
    """THE DIFFERENCE. `normalise` exists because some producers write {instance_id: bool}."""
    assert V.is_resolved(True) is True
    assert V.is_resolved(False) is False
    assert (True == "RESOLVED") is False, (
        "前提が崩れている -- 生比較が bool を取りこぼすことがこの修正の理由")


def test_case_and_whitespace_do_not_change_a_verdict():
    assert V.is_resolved(" resolved ") is True
    assert V.is_resolved("Resolved") is True


def test_a_non_measurement_is_never_resolved():
    for v in ("EVALERR", "NOPATCH", "", None):
        assert V.is_resolved(v) is False
        assert V.is_measurement(v) is False


# ── the promise, enforced ─────────────────────────────────────────────────────────────────

def test_no_file_compares_a_verdict_to_a_literal():
    """The eleventh copy fails here.

    Measured 2026-09-13 before the fix: ten sites in seven files -- pro_baseline_report,
    pro_ledger_report, pro_repeat_report, swe_check_remote, swe_grade_batch, swe_singleshot
    and swe_scorecard (four of them). Two of those files already imported the module and used
    `is_measurement` immediately above the literal.
    """
    offences = []
    for path in _bench_sources():
        base = os.path.basename(path)
        if base in _ALLOWED or base.startswith("test_"):
            continue
        src = open(path, encoding="utf-8", errors="replace").read()
        # A file that runs the value through the shared rule first has already removed the
        # three ways a literal goes wrong -- a boolean, a case difference, stray whitespace.
        # What is left there is BUCKETING an already-normalised word for a report, where an
        # unrecognised verdict lands in a visible "other" rather than being miscounted. That
        # is a different act from deciding whether a row is a measurement, and it is allowed.
        normalised_here = "_V.normalise(" in src
        for n, line in enumerate(src.splitlines(), 1):
            m = _LITERAL.search(_code_only(line))
            if not m:
                continue
            # RESOLVED is never allowed: that decision is `is_resolved`'s, normalised or not.
            if m.group(1) == _MEASUREMENT_WORD or not normalised_here:
                offences.append("%s:%d  %s"
                                % (os.path.relpath(path, REPO), n, line.strip()[:90]))
    assert not offences, (
        "判定語との直接比較が %d 箇所ある。bench/verdicts.py の is_resolved / is_measurement を"
        "通すこと（生比較は bool の True を『未解決』と読む）:\n  %s"
        % (len(offences), "\n  ".join(offences)))


def test_the_shared_rule_is_actually_imported_where_it_is_used():
    """A file that stopped comparing literals but never imported the module would fail at
    runtime rather than here, and only on the path that reaches that line."""
    for path in _bench_sources():
        src = open(path, encoding="utf-8", errors="replace").read()
        if "_V.is_resolved(" in src or "_V.is_measurement(" in src:
            assert "verdicts as _V" in src, (
                "%s は _V を使っているのに verdicts を import していない"
                % os.path.relpath(path, REPO))
