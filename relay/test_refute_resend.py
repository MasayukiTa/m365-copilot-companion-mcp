# -*- coding: utf-8 -*-
"""The REFUTED branch used to put byte-identical text on the wire twice.

MEASURED, not suspected. Over 1,756 transcripts there were 303 consecutive reviewer-remark
pairs; 81 of them were byte-identical, and every one of those 81 had NO worker response
between them. So none of the 81 was a refuter repeating itself -- the refuter never once
returned the same remark (the 222 differing pairs have a median Jaccard of 0.08). All 81 were
this branch re-sending `self.job` after a deferred send.

Two harms, both already on this project's record:
  * byte-identical re-injection degrades the M365 Copilot model until it refuses to answer
    (~5 repeats, observed live -- the reason _continue_nudge was made to escalate), and
  * every "how many refute rounds did this take" measurement counts a re-send as a round, so
    round counts are inflated until the two are separable.

The CONTINUE branch was cured of exactly this and the REFUTED branch was left on the old
constant. These tests pin the cure on the second branch.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.copilot_autopilot_relay import REFUTE_FIX_JOB  # noqa: E402
from relay.relay_fleet import _refute_fix_job  # noqa: E402


def test_the_first_send_is_unchanged():
    """Back-compat: a refute round that is sent once must look exactly as it always did, or
    every transcript comparison against history breaks for no reason."""
    assert _refute_fix_job("boundary case missed", 1) == REFUTE_FIX_JOB % "boundary case missed"


def test_a_missing_reason_still_reads_as_it_did():
    assert _refute_fix_job("", 1) == REFUTE_FIX_JOB % "(no reason)"
    assert _refute_fix_job(None, 1) == REFUTE_FIX_JOB % "(no reason)"


def test_a_resend_is_never_byte_identical_to_the_send_before_it():
    """THE DEFECT. Consecutive sends of one reason must differ, whatever the count."""
    reason = "the empty-input case is still unhandled"
    seen = [_refute_fix_job(reason, n) for n in range(1, 12)]
    for a, b in zip(seen, seen[1:]):
        assert a != b, "two consecutive sends of one reason are byte-identical"


def test_the_reason_survives_every_resend():
    """Varying the wrapper must not lose the thing the worker has to act on."""
    reason = "the empty-input case is still unhandled"
    for n in range(1, 12):
        assert reason in _refute_fix_job(reason, n)


def test_a_resend_says_it_is_one():
    """The worker should be able to tell a repeat from a new finding -- it wrote 'same remark
    re-sent' in a live transcript, having worked it out for itself."""
    later = _refute_fix_job("x", 2)
    assert "再送" in later and "2" in later


def test_the_count_does_not_run_off_the_end():
    """A long deferral streak must not raise; the phrases cycle."""
    for n in range(1, 60):
        assert _refute_fix_job("x", n)


def test_a_new_round_starts_over():
    """Attempt 1 of a LATER round is still the plain constant -- the counter belongs to one
    reason, not to the worker's lifetime."""
    assert _refute_fix_job("second finding", 1) == REFUTE_FIX_JOB % "second finding"
