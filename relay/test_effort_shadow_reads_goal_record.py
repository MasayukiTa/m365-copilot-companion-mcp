# -*- coding: utf-8 -*-
"""shadow_tick must read the goal DICT (worker.goal_record), not the goal TEXT (worker.goal).

An explicit per-goal effort sets the shadow initial level and floor (precedence goal > run).
Text-only goals and absent effort fall back to the run level and never crash.
"""
import os
import sys
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import effort as effort_mod          # noqa: E402
from relay import effort_policy as ep           # noqa: E402

ENV = {"MCP_EFFORT_POLICY": "shadow"}


def _worker(run_level="max", goal_record=None, goal="some goal text"):
    k = effort_mod.LEVELS[run_level]
    return SimpleNamespace(
        name="w0", run_id="r", goal=goal, goal_record=goal_record, status="running",
        outcome="", turn=1, max_turns=10, refuter=k["refuter"], max_refute=k["max_refute"],
        max_research=k["max_research"], review_lenses=list(k["review_lenses"] or []))


def _tick(w):
    recs = []
    ep.shadow_tick(w, record=lambda *a, **kw: recs.append(kw), env=ENV)
    return recs


@pytest.mark.parametrize("level", ["min", "max", "auto", "ultra"])
def test_explicit_goal_effort_sets_initial_level_and_floor(level):
    w = _worker(run_level="max" if level != "max" else "min",
                goal_record={"text": "x", "effort": level})
    recs = _tick(w)
    assert recs and recs[0]["config_value"] == level
    assert w._effort_shadow["state"].floor == level
    assert w.effort_state.level == level


def test_metadata_effort_is_honoured():
    w = _worker(run_level="min", goal_record={"text": "x", "metadata": {"effort": "ultra"}})
    _tick(w)
    assert w.effort_state.floor == "ultra" and w.effort_state.level == "ultra"


def test_absent_effort_falls_back_to_run_level():
    w = _worker(run_level="auto", goal_record={"text": "x"})
    _tick(w)
    assert w.effort_state.level == "auto" and w.effort_state.floor == "min"


@pytest.mark.parametrize("rec", [None, {}, "plain text", 5])
def test_text_only_or_malformed_goal_does_not_crash(rec):
    w = _worker(run_level="max", goal_record=rec)
    assert _tick(w)
    assert w.effort_state.level == "max" and w.effort_state.floor == "min"


def test_goal_text_is_never_read_as_the_goal():
    # A dict in .goal (legacy shape) must NOT be what shadow_tick consults.
    w = _worker(run_level="max", goal_record={"text": "x"}, goal={"effort": "ultra"})
    _tick(w)
    assert w.effort_state.floor == "min"


def test_goal_dict_is_the_single_accessor():
    w = _worker(goal_record={"effort": "min"})
    assert ep.goal_dict(w) == {"effort": "min"}
    assert ep.goal_dict(SimpleNamespace(goal="t")) == {}
