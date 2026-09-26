# -*- coding: utf-8 -*-
"""A useful candidate DONE must not be erased by an unanswered refuter-fix turn.

Measured run r6ab7d72e_a0: turn 5 returned a substantive audit ending DONE; the reviewer asked
for a correction, and the correction turn never replied. The generic transient path then spent
eight 240-second retries and replaced a useful completed result with STUCK.  The reviewer has
already contradicted the claim, so the truthful terminal state is EVIDENCE_CONTRADICTED: work
finished, not a pass, and not something the retry loop should resurrect automatically.
"""
from __future__ import annotations

import time

from relay.relay_fleet import RelayWorker


class _Answers:
    def __init__(self, n=0): self.n = n
    def count(self): return self.n


class _SilentDriver:
    def __init__(self): self.answers = _Answers(0)
    def _answers(self): return self.answers


def _waiting_candidate():
    w = RelayWorker("audit goal", "w0", refuter=False)
    w.drv = _SilentDriver()
    w._capture_url = lambda: None
    w.status = "waiting"
    w._count_before = 0
    w._t_send = time.time() - 300
    w.per_turn_timeout_s = 240
    w._candidate_done_reply = "useful audit result\nDONE"
    w._candidate_done_turn = 5
    w._refute_reason = "requested report file was not persisted"
    w._refute_fix_pending = True
    return w


def test_unanswered_refuter_fix_preserves_candidate_instead_of_spending_transient_retries():
    w = _waiting_candidate()
    terminal = w.poll()
    assert terminal is True
    assert w.status == "done"
    assert w.outcome == "EVIDENCE_CONTRADICTED"
    assert w.transient == 0, "do not enter the generic 8-retry outage budget after candidate DONE"
    assert w.last_response == "useful audit result\nDONE"
    assert "refuter" in w.reason.lower() or "review" in w.reason.lower()
    assert "requested report file" in w.reason
    assert w.retryable_override is False


def test_ordinary_unanswered_turn_keeps_the_existing_transient_retry_policy():
    w = _waiting_candidate()
    w._refute_fix_pending = False
    w._candidate_done_reply = ""
    terminal = w.poll()
    assert terminal is False
    assert w.status == "ready"
    assert w.transient == 1
    assert "turn timeout -> retry" in w.reason


def test_done_claim_is_saved_as_the_candidate_before_review():
    w = RelayWorker("audit goal", "w0", refuter=False)
    w._on_done_claimed("full useful result\nDONE")
    assert w._candidate_done_reply == "full useful result\nDONE"
    assert w._candidate_done_turn == w.turn


def test_any_real_fix_reply_closes_the_unanswered_refute_window():
    w = RelayWorker("audit goal", "w0", refuter=False)
    w._refute_fix_pending = True
    w._candidate_done_reply = "old candidate\nDONE"
    w._refute_reason = "missing artifact"
    w._decide("I am continuing the correction. CONTINUE")
    assert w._refute_fix_pending is False
    assert w.status != "done" or w.outcome != "EVIDENCE_CONTRADICTED"
