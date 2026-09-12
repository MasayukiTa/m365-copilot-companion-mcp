# -*- coding: utf-8 -*-
"""The repeat counter steered the run and never reached the ledger.

`self.no_progress` counts consecutive replies whose normalised key is identical, and
`NET_RETRY_NOPROGRESS_MAX` uses it to end a dead endpoint early instead of waiting out the full
retry window. A control input with no record: nothing under `.fleet` said how often it rose,
how far, or on which worker -- so "does this signal see the stalls that happen" could only be
answered by reprocessing transcripts, and not at all once retention has compressed or pruned
them.

WHAT THIS IS NOT. It is not a port of deepseek-harness's `repeat-tool-reminder`. That plugin
keys on tool name plus canonicalised arguments; this process observes neither, because the tool
calls happen inside Copilot. The review that recommended looking at that plugin said exactly
this, and said to observe response repetition first with no intervention, dropping its
`[3,5,8]` thresholds as having no basis here.

AND THE GAP IT WOULD ADDRESS COULD NOT BE MEASURED. Over 1,832 archived transcripts: 285
(15.6%) carry a verbatim repeat, 197 are long and end without DONE, and 75 of those have no
verbatim repeat -- but reading them, most are a deliberate `STUCK:` (a worker correctly
refusing to fabricate) or a finished answer that did not happen to end with the DONE token. The
proxy does not isolate a paraphrase-stall, so its size is unknown and a detector for it would
have been built on nothing. Recording the signal that exists is what makes the question
answerable from operational data later.

NOTHING HERE STEERS. The early-exit threshold and every branch around it are untouched.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as RF  # noqa: E402


def _source_without_comments() -> str:
    with open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8") as fh:
        src = fh.read()
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


def _decide_region(body: str) -> str:
    i = body.index("self.no_progress = self.no_progress + 1")
    return body[i - 200:i + 1200]


# ── the record ────────────────────────────────────────────────────────────────────────────

def test_a_repeat_is_recorded_with_its_run_length():
    body = _decide_region(_source_without_comments())
    assert 'metric(self.turn, "reply_repeat", self.no_progress' in body, (
        "the repeat counter still leaves no record; its coverage cannot be measured from "
        "operational data")


def test_nothing_is_recorded_when_the_reply_changed():
    """A row per turn saying "no repeat" is the denominator of a question nobody asked, and the
    turn count is already in the transcript."""
    body = _decide_region(_source_without_comments())
    i = body.index('metric(self.turn, "reply_repeat"')
    before = body[:i]
    assert "if self.no_progress:" in before[-260:], (
        "the record is not gated on a repeat having happened")


def test_the_record_says_it_was_observed():
    """Same field as the measured timeout, for the same reason: when these rows are later
    joined with turn_outcome's inferences, a reader has to be able to tell which were seen."""
    body = _decide_region(_source_without_comments())
    i = body.index('metric(self.turn, "reply_repeat"')
    assert "observed=True" in body[i:i + 260]


def test_telemetry_on_the_reply_path_cannot_fail_the_turn():
    body = _decide_region(_source_without_comments())
    i = body.index('metric(self.turn, "reply_repeat"')
    assert "except Exception:" in body[i:i + 300], (
        "a transcript write on the reply path can now raise and lose the reply")


# ── what must not have changed ────────────────────────────────────────────────────────────

def test_the_counter_itself_is_unchanged():
    """OBSERVE, DO NOT STEER. Adding a record to a signal that already drives control, while
    also changing what it drives, would make both unevaluable."""
    body = _source_without_comments()
    assert ("self.no_progress = self.no_progress + 1 if norm and norm == self.last_norm else 0"
            in body)
    assert "if self.no_progress >= NET_RETRY_NOPROGRESS_MAX:" in body


def test_the_normalisation_still_strips_the_volatile_fields():
    """The key once compared the first 300 raw characters, and a canned error page spends its
    first 130 on a conversation guid and a UTC timestamp -- so five byte-identical rate-limit
    errors compared as five different replies and the early exit never fired in 110 such
    transcripts. This is the half of the signal that makes the counter mean anything."""
    a = RF._norm_for_progress(
        "Conversation 1a2c4e8d-89bf-4877-9876-cb1ea0f2a184 at 2026-09-13T01:02:03Z: failed")
    b = RF._norm_for_progress(
        "Conversation 9f3e0011-2222-4877-0000-aaaabbbbcccc at 2026-09-13T04:05:06Z: failed")
    assert a == b, "two identical errors with different guids/timestamps still compare unequal"


def test_a_genuinely_different_reply_is_not_a_repeat():
    assert RF._norm_for_progress("found the file") != RF._norm_for_progress("no file found")
