# -*- coding: utf-8 -*-
"""`_wants_fanout` answers whether a run is fan-out-CAPABLE. It stopped deciding anything else.

HISTORY, BECAUSE THE TESTS BELOW USED TO SAY THE OPPOSITE. It began as a length proxy; codex-plan
item 6 replaced that with `splittability.judge` after measuring the proxy called SPLIT on 60.8%
of a 1214-goal corpus against 0.7% by independence. Both versions had it DECIDING -- answering
"no" for a whole run when no goal looked splittable.

That was wrong in a way neither version fixed, and 2026-09-13 removed it. RelayWorker runs the
same judge per goal, later and with more information, so running it here could only take the
question away from the real judge -- for every goal in the run, including goals added mid-run
that nobody had seen. It is also the one answer that cannot be revisited: a goal judged
NO_SPLIT costs no turn, so saying yes here costs nothing, while saying no costs the capability
outright.

So the capability is on, and the only ways to turn it off are an operator saying so
(FLEET_INTAKE_AUTOSTART_FANOUT) or the documented kill switch (AUTOSTART_FANOUT_MIN_CHARS <= 0).
The claims the deleted tests protected -- "long is not splittable", "an ordinary request is not
splittable" -- still hold and are asserted where they now live, in relay/test_splittability.py
and in the per-goal eligibility tests, rather than through a function that no longer consults
the judge at all.
"""
import pytest

from relay import task_router as TR


def test_the_capability_is_on_without_anyone_asking():
    """Including for a batch where nothing looks splittable. Costing nothing is the point: the
    per-goal judge answers NO_SPLIT for these and no turn is spent."""
    assert TR._wants_fanout([{"text": "1+1はいくつですか"},
                             {"text": "デスクトップの一覧を出して"}]) is True


def test_a_long_string_with_no_independence_signal_is_still_not_split():
    """The claim the old length-proxy test protected, asserted where it now lives. `x` repeated
    past the old threshold has no independence signal; the judge -- which is what RelayWorker
    consults -- must not call it SPLIT just for being long."""
    from relay import splittability as sp
    assert sp.judge("x" * 700).decision != sp.SPLIT


def test_an_empty_batch_is_still_capable():
    """A run's goals can arrive after it starts. Answering from a batch that is not yet there
    is how a mid-run goal loses its capability before anyone has read it."""
    assert TR._wants_fanout([]) is True
    assert TR._wants_fanout(None) is True


def test_explicit_override_true_wins_regardless_of_goal_content(monkeypatch):
    monkeypatch.setenv("FLEET_INTAKE_AUTOSTART_FANOUT", "1")
    assert TR._wants_fanout([{"text": "x"}]) is True


def test_explicit_override_false_wins_regardless_of_goal_content(monkeypatch):
    monkeypatch.setenv("FLEET_INTAKE_AUTOSTART_FANOUT", "0")
    assert TR._wants_fanout([{"text": "1〜3月のメールを一覧化する"}]) is False


def test_threshold_disabled_gives_false_even_for_a_splittable_goal(monkeypatch):
    monkeypatch.setenv("FLEET_INTAKE_AUTOSTART_FANOUT_MIN_CHARS", "0")
    import importlib
    importlib.reload(TR)
    try:
        assert TR._wants_fanout([{"text": "1〜3月のメールを一覧化する"}]) is False
    finally:
        monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT_MIN_CHARS", raising=False)
        importlib.reload(TR)


def test_a_broken_splittability_import_cannot_disable_the_capability(monkeypatch):
    """The concern that produced the old length-proxy fallback, now answered by construction.

    A judging failure must not silently disable a whole run's fan-out -- unlike the per-goal
    case in RelayWorker, where a failure must default to NOT splitting. This function no longer
    imports the judge at all, so there is nothing here for an import hiccup to break."""
    monkeypatch.setattr(TR, "_splittability", None)
    assert TR._wants_fanout([{"text": "x" * 700}]) is True
    assert TR._wants_fanout([{"text": "x" * 10}]) is True
