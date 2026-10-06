# -*- coding: utf-8 -*-
"""Injecting unlock silently disabled the planner component.

`_initial_job_with_unlock` composed the first turn by hand whenever MCP_UNLOCK_PASSWORD exists
-- which is the normal configuration -- and only reached `opening_turn` when it did NOT. So the
whole `planner` component was bypassed in every ordinary run, and planner/v1 and planner/v2
produced byte-identical first turns: an A/B whose two arms are the same program.

That is the exact failure PLANNER_VERSIONS was created to end. The comment above that table says
so about its own predecessor, and the same hole was open one file over, in the branch nobody
compared because it only runs when a password is set.

ON THE PASSWORD ITSELF, which is a separate question and deliberately not changed here: the
survey write-up asked whether the credential could stay out of the message body. It cannot.
`unlock(password)` compares the argument against the server's own copy, so the argument is a
PROOF OF KNOWLEDGE, not a lookup -- a version that read the environment and self-approved would
let anyone holding the API key unlock, which is the two-factors-collapsed-into-one failure that
tools/security.py documents at its own unlock(). The credential has to reach the agent. What is
fixed here is the capability that was being dropped alongside it.

NOTHING HERE TOUCHES THE ENVIRONMENT. The first version of this file set MCP_UNLOCK_PASSWORD at
import time, which is precisely the hazard test_unlock_inject.py documents: pytest imports every
module before it runs anything, so an import-time env write is live for every other test until
the writing module's teardown, and two unrelated tests once failed in CI and passed locally on
import order alone. `_unlock_password()` reads its value on each call, so a per-test patch is
both sufficient and contained.

CHANGED 2026-09-25: the non-plan_mode branch (the normal, always-on fleet configuration) no
longer injects UNLOCK_PREFIX/the password into turn 1 at all -- M365 Copilot's own safety/DLP
filter refused that exact message shape deterministically in production, so the proactive send
could never succeed. Turn 1 is now always `opening_turn(goal, PROTOCOL)`, unconditionally --
which, as a side effect, means the planner-routing fix this file was written to pin down (both
arms reach `opening_turn`, so planner/v1 and planner/v2 differ) now holds unconditionally rather
than only "when no password is set". plan_mode (operator-set plan-then-wait) is untouched and
still injects proactively; see test_operator_plan_mode_is_not_reinterpreted below.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import planner as P        # noqa: E402
from relay import relay_fleet as F    # noqa: E402

PW = "planner_test_pw_zz9"
GOAL = "全国の劇場ごとに配布状況を調べて"


@pytest.fixture
def held(monkeypatch):
    """A password exists, without writing one into the process environment."""
    monkeypatch.setattr(F, "_unlock_password", lambda: PW)


@pytest.fixture
def planner(monkeypatch):
    """Pin the planner version the way the harness's component table does."""
    def use(version):
        monkeypatch.setattr(F, "opening_turn",
                            lambda goal, protocol: P.PLANNER_VERSIONS[version](goal, protocol))
    return use


def test_v1_no_longer_injects_password_into_turn_one(held, planner):
    """CHANGED 2026-09-25: turn 1 never carries UNLOCK_PREFIX/the password anymore, under either
    planner version -- M365 Copilot's safety filter refused that exact shape deterministically,
    so nothing is gained by putting it there and the send is no longer made."""
    planner("planner/v1")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert not injected
    assert got == F.PROTOCOL + GOAL
    assert PW not in got


def test_v2_now_actually_differs(held, planner):
    """THE POINT, still true after the 2026-09-25 change: both arms now route through
    `opening_turn` UNCONDITIONALLY (not only when no password is set), so planner/v1 and
    planner/v2 still produce different first turns."""
    planner("planner/v1")
    v1, _ = F._initial_job_with_unlock(GOAL)
    planner("planner/v2")
    v2, _ = F._initial_job_with_unlock(GOAL)
    assert v1 != v2, "planner/v1 and planner/v2 still produce the same first turn"
    assert P.PLAN_PROMPT in v2 and P.PLAN_PROMPT not in v1


def test_the_credential_no_longer_reaches_turn_one(held, planner):
    """CHANGED 2026-09-25: the credential must NOT appear in turn 1 anymore. It still reaches
    the agent, but only reactively (_inject_unlock), once a genuine write/exec lock refusal is
    actually observed in a reply -- see test_unlock_inject.py."""
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert not injected
    assert PW not in got


def test_the_goal_still_survives_intact(held, planner):
    planner("planner/v2")
    got, _ = F._initial_job_with_unlock(GOAL)
    assert GOAL in got


def test_operator_plan_mode_is_not_reinterpreted(held, planner):
    """plan_mode is plan-then-WAIT, set by a person. A component version does not get to
    quietly redefine a flag the operator sets by hand, so that path is left alone."""
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL, plan_mode=True)
    assert injected
    assert got == F.PROTOCOL + (F.UNLOCK_PREFIX % PW) + P.PLAN_PROMPT + GOAL


def test_without_a_password_the_planner_was_always_honoured(monkeypatch, planner):
    """The branch that already worked, kept working -- this is the one the A/B was measuring
    when it believed it was measuring both."""
    monkeypatch.setattr(F, "_unlock_password", lambda: "")
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert not injected
    assert P.PLAN_PROMPT in got
