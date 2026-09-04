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
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PW = "planner_test_pw_zz9"
_PREV = os.environ.get("MCP_UNLOCK_PASSWORD")
os.environ["MCP_UNLOCK_PASSWORD"] = PW    # before the import, or .env is read instead

from relay import relay_fleet as F        # noqa: E402
from relay import planner as P            # noqa: E402


def teardown_module(_m):
    if _PREV is None:
        os.environ.pop("MCP_UNLOCK_PASSWORD", None)
    else:
        os.environ["MCP_UNLOCK_PASSWORD"] = _PREV


GOAL = "全国の劇場ごとに配布状況を調べて"


@pytest.fixture
def planner(monkeypatch):
    """Pin the planner version the way the harness does."""
    def use(version):
        monkeypatch.setattr(P, "opening_turn",
                            lambda goal, protocol: P.PLANNER_VERSIONS[version](goal, protocol))
        monkeypatch.setattr(F, "opening_turn",
                            lambda goal, protocol: P.PLANNER_VERSIONS[version](goal, protocol))
    return use


def test_v1_is_byte_identical_to_what_it_produced_before(planner):
    """The fix must not move a single character under the version that was already running,
    or it is a behaviour change hiding inside a bug fix."""
    planner("planner/v1")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert injected
    assert got == F.PROTOCOL + (F.UNLOCK_PREFIX % PW) + GOAL


def test_v2_now_actually_differs(planner):
    """THE POINT. Before this, the two arms returned the same string."""
    planner("planner/v1")
    v1, _ = F._initial_job_with_unlock(GOAL)
    planner("planner/v2")
    v2, _ = F._initial_job_with_unlock(GOAL)
    assert v1 != v2, "planner/v1 and planner/v2 still produce the same first turn"
    assert P.PLAN_PROMPT in v2 and P.PLAN_PROMPT not in v1


def test_the_credential_still_reaches_the_agent(planner):
    """It has to: unlock() proves knowledge rather than looking the value up."""
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert injected and PW in got


def test_the_goal_still_survives_intact(planner):
    planner("planner/v2")
    got, _ = F._initial_job_with_unlock(GOAL)
    assert GOAL in got


def test_operator_plan_mode_is_not_reinterpreted(planner):
    """plan_mode is plan-then-WAIT, set by a person. A component version does not get to
    quietly redefine a flag the operator sets by hand, so this path is left alone."""
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL, plan_mode=True)
    assert injected
    assert got == F.PROTOCOL + (F.UNLOCK_PREFIX % PW) + P.PLAN_PROMPT + GOAL


def test_without_a_password_the_planner_was_always_honoured(monkeypatch, planner):
    """The branch that already worked, kept working -- this is the one the A/B was measuring
    when it thought it was measuring both."""
    monkeypatch.delenv("MCP_UNLOCK_PASSWORD", raising=False)
    monkeypatch.setattr(F, "_unlock_password", lambda: "")
    planner("planner/v2")
    got, injected = F._initial_job_with_unlock(GOAL)
    assert not injected
    assert P.PLAN_PROMPT in got
