# -*- coding: utf-8 -*-
"""A goal with no acceptance check got the same thousand-turn budget as a coding task.

From the 290-cinema survey write-up of 2026-09-04: "検証条件(checks)のない単純な情報収集タスクにも、
コーディングタスク向けの巨大な上限がそのまま使われている".

WHY THE TWO ARE NOT THE SAME. With checks there is something that can go from red to green, so
another turn can convert into a result. Without them nothing can change state; more turns only
produce more prose about the same evidence.

WHAT THIS DOES NOT DO, said plainly: the cap has never bound. Across the stored transcripts the
highest turn any worker ever reached is eleven, against a default of a thousand. No run gets
shorter today. What changes is the cost when something else has already failed -- a stall
detector that stops firing costs forty turns on an unverifiable task instead of a thousand.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import relay_fleet as F  # noqa: E402


def cap_for(goal, run_cap=1000):
    """The cap a worker would be built with, without building one."""
    _text, checks, _cwd = F.goal_fields(goal)
    if checks:
        return run_cap
    return (min(run_cap, F.UNVERIFIABLE_MAX_TURNS) if run_cap
            else F.UNVERIFIABLE_MAX_TURNS)


def test_a_goal_with_no_checks_is_bounded():
    """A plain string is the back-compat shape for 'no acceptance check'."""
    assert cap_for("全国の劇場ごとに配布状況を調べて") == F.UNVERIFIABLE_MAX_TURNS


def test_a_goal_with_checks_keeps_the_full_budget():
    """A long run on a verifiable task may still be converging on something real."""
    goal = {"text": "fix the failing test", "checks": [{"id": "t", "command": "pytest -x"}]}
    assert cap_for(goal) == 1000


def test_the_run_cap_still_wins_when_it_is_tighter():
    """An operator who asked for 10 turns gets 10, not 40."""
    assert cap_for("調べて", run_cap=10) == 10


def test_unlimited_still_bounds_the_unverifiable_case():
    """max_turns=0 means unlimited. Unlimited is exactly where an unverifiable goal should not
    be: nothing will ever go green to stop it."""
    assert cap_for("調べて", run_cap=0) == F.UNVERIFIABLE_MAX_TURNS


def test_unlimited_is_still_unlimited_where_something_can_go_green():
    goal = {"text": "fix it", "checks": [{"id": "t", "command": "pytest"}]}
    assert cap_for(goal, run_cap=0) == 0


def test_the_bound_is_well_clear_of_anything_ever_observed():
    """Eleven is the highest turn any stored transcript reached. A cap that could bite in
    normal operation would be a behaviour change wearing a safety net's clothes."""
    assert F.UNVERIFIABLE_MAX_TURNS >= 30


@pytest.mark.parametrize("shape", [
    {"text": "x", "check": {"id": "t", "command": "pytest"}},   # singular key
    {"goal": "x", "checks": [{"id": "t", "command": "pytest"}]},  # 'goal' instead of 'text'
])
def test_every_shape_that_carries_a_check_is_recognised(shape):
    """goal_fields accepts several spellings; missing one would silently bound a coding task."""
    assert cap_for(shape) == 1000
