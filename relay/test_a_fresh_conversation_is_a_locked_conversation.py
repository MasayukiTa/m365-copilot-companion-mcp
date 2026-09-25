# -*- coding: utf-8 -*-
"""The two branches that open a chat with no history no longer inject the password proactively.

WHAT THE LEDGER SAID, 2026-09-15. `.fleet/lock_refusals.jsonl`, 4,271 refusals since
2026-08-22:

    "[locked: no valid unlock token"   4258   (99.7%)
    "[locked: no HTTP request context]"   6
    "[locked client IP:"                  7

One branch is essentially the whole population. Its meaning is specific: the client IP IS
unlocked and holds tokens -- 128 of them for this identity -- but the call presented none.
Of the ten most recent, every single one was a session that had never called unlock BEFORE
being refused, and three never called it afterwards either.

That ledger is why `_recycle_job`/`_replay_job` were originally made to carry UNLOCK_PREFIX
proactively into the opening turn of a recycled or replayed conversation: authorization is
per MCP session by design, so a fresh conversation starts locked, and waiting for the refusal
before reacting meant the recovery mostly did not arrive (of 518 refusals whose session was
recorded, 453 never saw a successful unlock in that session again).

CHANGED 2026-09-25. That reasoning about the session boundary was correct, but the fix it
produced repeated the exact defect `_initial_job_with_unlock` had at turn 1: production
transcripts (.fleet/transcripts/r6ab5aa80_a0_w0.jsonl and others) showed Microsoft 365
Copilot's own safety/DLP filter refusing "call a tool with a password argument" as an opening
message, byte-identically, every time -- deterministic, not transient. Putting the password
into the opening turn of a recycled or replayed conversation is the same shape turn 1 had; it
does not avoid the refusal, it relocates it one conversation later. Both branches now send the
plain re-anchored goal (protocol + procedure + goal, no password) as their opening turn, and
rely on the same reactive path turn 1 now relies on: `_looks_locked()` -> `_inject_unlock()`,
fired from `_decide_impl` the first time a write/exec tool is actually refused -- a shape
Copilot does not blanket-refuse.

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


def test_a_recycled_conversation_no_longer_carries_the_unlock(with_password):
    """CHANGED 2026-09-25: a recycle's opening turn must NOT contain the password -- Copilot's
    safety filter blanket-refuses that shape regardless of which conversation it opens. The
    reactive path re-unlocks after a genuine refusal instead; see test_unlock_inject.py."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert _unlock_text() not in job, (
        "a recycled conversation's opening turn must stay password-free, same as turn 1")
    assert "password=" not in job


def test_a_replayed_conversation_no_longer_carries_the_unlock(with_password):
    """Same class of fix one method up. Fixing only the recycle would have left the identical
    hole open here."""
    w = F.RelayWorker(GOAL, "w0")
    w.fresh_replay_count = 1
    job = w._replay_job()
    assert _unlock_text() not in job
    assert "password=" not in job


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


def test_the_procedure_still_travels(with_password):
    """The property the previous test file holds. Removing the proactive unlock must not
    displace it."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert w._composed_prefix in job


def test_no_password_means_no_unlock_text_rather_than_a_crash(without_password):
    """A machine with no MCP_UNLOCK_PASSWORD must still get a usable re-anchor -- and, since
    neither branch injects proactively any more, this now holds regardless of whether a
    password is configured at all."""
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    job = w._recycle_job()
    assert job.count(GOAL) == 1
    assert "password=" not in job
    w.fresh_replay_count = 1
    assert w._replay_job().endswith(GOAL)


def test_a_recycle_resets_the_reactive_budget_to_zero(with_password):
    """MAX_UNLOCK_ATTEMPTS bounds a re-unlock loop WITHIN one conversation, where repeated
    failure means the password or the identity is wrong. A recycle is a different
    conversation and is separately bounded by _max_recycles, so charging it here would make a
    long healthy job go STUCK for "unlock attempts exhausted" with nothing having failed.

    CHANGED 2026-09-25: this used to reset to 1, counting the proactive injection this
    function performed. There is no longer a proactive injection to count, so the reset is to
    0 -- the reactive path's own budget, spent only if the recycled conversation is actually
    refused."""
    w = F.RelayWorker(GOAL, "w0")
    w._unlock_attempts = F.MAX_UNLOCK_ATTEMPTS
    w._recycles = 1
    w._recycle_job()
    assert w._unlock_attempts == 0
    assert w._unlock_attempts < F.MAX_UNLOCK_ATTEMPTS, (
        "a recycled worker starts with no room to recover from a genuine re-lock")


def test_a_replay_resets_the_reactive_budget_to_zero(with_password):
    """Same reasoning as the recycle test above, for the sibling branch."""
    w = F.RelayWorker(GOAL, "w0")
    w._unlock_attempts = F.MAX_UNLOCK_ATTEMPTS
    w.fresh_replay_count = 1
    w._replay_job()
    assert w._unlock_attempts == 0


def test_the_reactive_path_still_composes_the_unlock_the_same_way(with_password):
    """So a fresh conversation that DOES get refused inherits the reactive path's handling
    rather than needing its own template.

    CHANGED 2026-09-25: recycle/replay used to inject the unlock text into their own opening
    turn, composed with the same template as the reactive path -- this test used to check that
    the two compositions matched. Now neither branch injects proactively at all (same bug
    shape turn 1 had, fixed the same way -- see the module docstring), so the property left to
    check is narrower: the reactive path (`_inject_unlock`, the only place a fresh
    conversation's turn 1 gets unlock text from now) still composes UNLOCK_PREFIX with the
    real password, unaffected by this change."""
    expected = F.UNLOCK_PREFIX % F._unlock_password()
    w = F.RelayWorker(GOAL, "w0")
    w._recycles = 1
    w.fresh_replay_count = 1
    assert expected not in w._recycle_job()
    assert expected not in w._replay_job()
    w2 = F.RelayWorker(GOAL, "w1")
    w2._inject_unlock()
    assert expected in w2.job, "the reactive path no longer composes it this way"


def test_turn_one_itself_no_longer_injects(with_password):
    """CHANGED 2026-09-25: the very first turn of a fresh worker must NOT carry the password --
    see test_unlock_inject.py and test_unlock_keeps_the_planner.py for the full contract."""
    w = F.RelayWorker(GOAL, "w0")
    initial, injected = F._initial_job_with_unlock(w._composed_goal, False)
    assert not injected
    assert PW not in initial
