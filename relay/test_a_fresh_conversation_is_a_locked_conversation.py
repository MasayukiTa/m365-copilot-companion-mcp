# -*- coding: utf-8 -*-
"""The two branches that open a chat with no history must carry the unlock.

WHAT THE LEDGER SAID, 2026-09-15. `.fleet/lock_refusals.jsonl`, 4,271 refusals since
2026-08-22:

    "[locked: no valid unlock token"   4258   (99.7%)
    "[locked: no HTTP request context]"   6
    "[locked client IP:"                  7

One branch is essentially the whole population. Its meaning is specific: the client IP IS
unlocked and holds tokens -- 128 of them for this identity -- but the call presented none.
Of the ten most recent, every single one was a session that had never called unlock BEFORE
being refused, and three never called it afterwards either.

WHY. Authorization is per MCP session by design, and _composed_prefix is sliced off
composed_goal, which is built BEFORE _initial_job_with_unlock prepends UNLOCK_PREFIX. So a
recycled or replayed conversation was rebuilt with the memory, the skill and the contract --
and no unlock. A fresh conversation is a fresh session, so the token the agent held died
with the old chat, and the new one was locked from its first gated call.

Measured on run r6aa92e5a: one worker, 34 minutes, 4 distinct MCP sessions. The two that
were never authorized never called unlock at all, and all three of its refusals were the
brand-new-session case rather than any session ageing out. The worker pressed the same key
nine times, because three of those nine never reached the keyboard at all.

Waiting for the refusal and then injecting was recovery from a certainty, and the ledger
says the recovery mostly did not arrive: of 518 refusals whose session was recorded, 453
never saw a successful unlock in that session again; the 65 that did took a median of 128
seconds and 5 further tool calls.

These tests hold the property both branches now have, and they call the builders the code
calls rather than reassembling the strings -- an assertion about my arithmetic would keep
passing after the branch stopped using them.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402

GOAL = "テスト用のゴール本文"
PW = "test-placeholder-not-a-real-password"


@pytest.fixture
def with_password(monkeypatch):
    """A password must exist for an unlock to be composable at all."""
    monkeypatch.setattr(F, "_unlock_password", lambda: PW)
    return PW


@pytest.fixture
def without_password(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "")


def _unlock_text():
    return F.UNLOCK_PREFIX % PW


def test_a_recycled_conversation_carries_the_unlock(with_password):
    """The 99.7% branch. A recycle is a new session, so the old token is gone by definition."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    assert _unlock_text() in w._recycle_job(), (
        "a recycled conversation opens locked; this is the refusal that is 4,258 of 4,271")


def test_a_replayed_conversation_carries_the_unlock(with_password):
    """The same class of defect one method up. Fixing only the recycle leaves it open."""
    w = F.RelayWorker(GOAL, "w0")
    w.fresh_replay_count = 1
    assert _unlock_text() in w._replay_job()


def test_the_goal_is_intact_and_nothing_precedes_it_but_context(with_password):
    """RECYCLE_PREFIX ends with a heading that introduces the goal, so the goal must follow
    it whole and exactly once.

    THIS ASSERTED endswith(GOAL) AND WOULD HAVE HELD BY ACCIDENT. A compaction note is now
    appended after the goal, so the job no longer ends with it -- and this test still passed,
    because a freshly constructed worker has written no transcript yet and the note came back
    empty. A test that holds only because its fixture is empty is not holding anything.

    The docstring also blamed _composed_prefix's suffix slice, which is wrong: that slice is
    taken from composed_goal in __init__, not from this job. What actually matters is that
    the goal arrives once and unbroken, which is what is asserted now."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert job.count(GOAL) == 1, "the goal was sent twice"
    from relay.copilot_autopilot_relay import RECYCLE_PREFIX
    assert job.index(RECYCLE_PREFIX) < job.index(GOAL)


def test_the_unlock_precedes_the_reset_notice(with_password):
    """Ordering matters for the same reason it does at turn 1: the unlock belongs with the
    protocol, above the notice whose heading introduces the goal."""
    from relay.copilot_autopilot_relay import RECYCLE_PREFIX

    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert job.index(_unlock_text()) < job.index(RECYCLE_PREFIX)


def test_the_procedure_still_travels(with_password):
    """The property the previous test file holds. Adding the unlock must not displace it."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert w._composed_prefix in job


def test_no_password_means_no_unlock_text_rather_than_a_crash(without_password):
    """A machine with no MCP_UNLOCK_PASSWORD must still get a usable re-anchor."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert job.count(GOAL) == 1
    assert "password=" not in job
    w.fresh_replay_count = 1
    assert w._replay_job().endswith(GOAL)


def test_a_recycle_resets_the_reactive_budget_instead_of_spending_it(with_password):
    """MAX_UNLOCK_ATTEMPTS bounds a re-unlock loop WITHIN one conversation, where repeated
    failure means the password or the identity is wrong. A recycle is a different
    conversation and is separately bounded by _max_recycles, so charging it here would make a
    long healthy job go STUCK for "unlock attempts exhausted" with nothing having failed."""
    w = F.RelayWorker(GOAL, "w0")
    w._unlock_attempts = F.MAX_UNLOCK_ATTEMPTS
    w._recycles = 1
    w._recycle_job()
    assert w._unlock_attempts == 1
    assert w._unlock_attempts < F.MAX_UNLOCK_ATTEMPTS, (
        "a recycled worker starts with no room to recover from a genuine re-lock")


def test_the_unlock_text_is_the_same_text_turn_one_sends(with_password):
    """So it inherits turn 1's handling rather than needing its own.

    The first version of this test asserted the password was gone after
    _redact_unlock_password, which was never true for a fake one: redact_secrets removes the
    values it finds in the secret store, so a placeholder passes through untouched and the
    test was measuring its own fixture. The property that actually matters is that both
    fresh-conversation branches compose the unlock EXACTLY as the initial path does -- same
    template, same source for the password -- so whatever redaction, logging and handling
    turn 1 gets, these get too, with nothing to keep in step by hand.
    """
    expected = F.UNLOCK_PREFIX % F._unlock_password()
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    w.fresh_replay_count = 1
    assert expected in w._recycle_job()
    assert expected in w._replay_job()
    initial, injected = F._initial_job_with_unlock(w._composed_goal, False)
    assert injected and expected in initial, "the initial path no longer composes it this way"
