# -*- coding: utf-8 -*-
"""An outcome nobody considered was recorded as one considered and refused.

`relay/outcomes.py::is_retryable` exists to close exactly one hole, and its docstring says so:

    Unlisted outcomes raise, they do not fall through to 'no' -- 'not retryable' and 'not
    considered' were the same answer before, and the one outcome that actually occurs here
    (STUCK) was the one originally left out of the list.

It had no caller. The live retry decision was a raw membership test --

    relay/relay_fleet.py:6414   elif _oc not in RETRYABLE_OUTCOMES:

-- which answers False for anything outside the closed set, and the telemetry beside it then
recorded "outcome %s is not retryable", which reads as a decision somebody made. Same silence,
same file, as the one that cost a whole outcome once.

NOT CLOSED BY RAISING INTO THE RUN LOOP. That site is inside `run_relay_fleet`'s main loop, so
an uncaught `UnknownOutcome` would end a live fleet over one worker's typo'd outcome string --
a worse answer than declining to retry one goal. The unknown case is caught, treated as not
retryable exactly as before, and made VISIBLE. Behaviour for every KNOWN outcome is unchanged;
what changed is that an unknown one stops looking like a decision.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay.outcomes import OUTCOMES, RETRYABLE, UnknownOutcome, is_retryable  # noqa: E402

SRC = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()


# ── the predicate's contract ──────────────────────────────────────────────────────────────

def test_a_known_outcome_answers_the_same_as_the_set():
    for oc in sorted(OUTCOMES):
        assert is_retryable(oc) == (oc in RETRYABLE), oc


def test_an_unknown_outcome_refuses_rather_than_saying_no():
    """THE WHOLE POINT. A membership test cannot tell 'considered and refused' from
    'never considered', and the outcome that actually occurred -- STUCK -- was the one left
    out of the list the first time."""
    with pytest.raises(UnknownOutcome):
        is_retryable("TYPO_OUTCOME")


def test_stuck_is_in_the_list_it_was_once_left_out_of():
    assert "STUCK" in RETRYABLE


# ── the decision site now uses it ─────────────────────────────────────────────────────────

def test_the_retry_decision_goes_through_the_predicate():
    assert "_oc not in RETRYABLE_OUTCOMES" not in SRC, (
        "再試行の判定が生の集合判定に戻っている -- 語彙外の outcome が黙って"
        "『再試行不可』になる")
    assert "_retry_allowed(_oc, _w)" in SRC


def test_the_telemetry_agrees_with_the_decision():
    """The row beside the decision used to compute the same thing a second way. Two copies of
    one rule is how the retryable list came to be missing STUCK."""
    assert "triggered=(_oc in RETRYABLE_OUTCOMES)" not in SRC
    assert "triggered=_retry_allowed(_oc, _w)" in SRC


def test_an_unknown_outcome_does_not_end_the_run():
    """Raising here would kill a live fleet over one typo'd string. Not retrying one goal is
    recoverable; ending the run is not."""
    i = SRC.index("def _retry_allowed(outcome, worker):")
    body = SRC[i:SRC.index("\n    _reap_counter", i)]
    assert "except _UnknownOutcome:" in body
    assert "return False" in body
    assert "raise" not in body.split("except _UnknownOutcome:")[1]


def test_the_omission_is_printed_and_recorded():
    """Catching it and staying silent would be the original defect with an extra step."""
    i = SRC.index("def _retry_allowed(outcome, worker):")
    body = SRC[i:SRC.index("\n    _reap_counter", i)]
    assert "print(" in body and "omission rather than a decision" in body
    assert "_mt.record(" in body
    assert "ineligible_reason=" in body


def test_the_telemetry_import_is_local_like_every_other_one():
    """`_mt` is not a module-level name in this file. Without the local import the NameError
    would be swallowed by the surrounding except and the record would silently never be
    written -- the same silence this function exists to end, reintroduced inside it."""
    i = SRC.index("def _retry_allowed(outcome, worker):")
    body = SRC[i:SRC.index("\n    _reap_counter", i)]
    assert "from relay import mechanism_telemetry as _mt" in body


def test_it_falls_back_to_the_old_rule_when_outcomes_is_unavailable():
    """The import sits in the same try/except that already guards settings_autoretry. If that
    fails, the previous behaviour is the honest fallback -- refusing to retry anything would
    be a new policy arriving through an import error."""
    i = SRC.index("def _retry_allowed(outcome, worker):")
    body = SRC[i:SRC.index("\n    _reap_counter", i)]
    assert "if _retryable is None:" in body
    assert "return outcome in RETRYABLE_OUTCOMES" in body
