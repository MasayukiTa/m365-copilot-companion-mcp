# -*- coding: utf-8 -*-
"""relay.task_router._wants_fanout had NO tests at all before codex-plan item 6 (confirmed by
grep: no file anywhere referenced _wants_fanout or AUTOSTART_FANOUT_MIN_CHARS except
task_router.py itself). It decides whether an autostarted run's --fanout capability is turned
on; codex-plan item 6 replaced its length-only proxy with relay.splittability.judge. These
pin the new contract directly, including the override envvar (which predates this change and
must still win in both directions) and the fallback path if splittability fails to import.
"""
import pytest

from relay import task_router as TR


def test_a_length_only_string_no_longer_triggers_fanout():
    """The defect this item closes: an 'x' repeated past the old length threshold has no
    independence signal and must not trigger fan-out just by being long."""
    goals = [{"text": "x" * 700}]
    assert TR._wants_fanout(goals) is False


def test_a_genuinely_splittable_short_goal_triggers_fanout():
    goals = [{"text": "1〜3月のメールを一覧化する"}]
    assert len(goals[0]["text"]) < TR.AUTOSTART_FANOUT_MIN_CHARS
    assert TR._wants_fanout(goals) is True


def test_any_one_splittable_goal_in_the_batch_is_enough():
    goals = [{"text": "短い依頼"}, {"text": "1〜3月のメールを一覧化する"}]
    assert TR._wants_fanout(goals) is True


def test_no_goal_in_the_batch_is_splittable_gives_false():
    goals = [{"text": "1+1はいくつですか"}, {"text": "デスクトップの一覧を出して"}]
    assert TR._wants_fanout(goals) is False


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


def test_falls_back_to_the_length_proxy_if_splittability_is_unavailable(monkeypatch):
    """A judging failure must not silently DISABLE the whole run's fan-out capability --
    unlike the per-goal case in RelayWorker (where a failure must default to NOT splitting),
    refusing --fanout outright here would regress a shipped feature on an import hiccup. The
    documented fallback is the OLD proxy, not a blanket refusal."""
    monkeypatch.setattr(TR, "_splittability", None)
    goals = [{"text": "x" * 700}]
    assert TR._wants_fanout(goals) is True
    goals_short = [{"text": "x" * 10}]
    assert TR._wants_fanout(goals_short) is False
