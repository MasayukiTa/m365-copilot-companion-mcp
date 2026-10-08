# -*- coding: utf-8 -*-
"""The splittability judge knows how deep the asking worker is.

Before: any text carrying the child scope block was NO_SPLIT ("already a fan-out child"), so a
depth-1 child was never offered a split even with the effective depth at 2 -- depth 2 was
structurally unreachable. Now a child that may still split (0 < depth < effective maximum) is
judged on its OWN slice (the scope block's step), not on the parent goal and the boilerplate
every child carries. At effective depth 1 nothing changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout as fo                     # noqa: E402
from relay import relay_fleet as rf                # noqa: E402
from relay import splittability as sp              # noqa: E402

GOAL = "売上データを集計して報告書を作成する。形式はCSV。"
STEPS = ["1月分のデータを取得して集計する", "2月分のデータを取得して集計する"]

MULTI_JA = "1. 東日本の売上を集計する\n2. 西日本の売上を集計する\n3. 結果を表にまとめる"
MULTI_EN = "(1) fetch the east table (2) fetch the west table (3) fetch the north table"
SINGLE_JA = "2月分のデータを取得して集計する"
SINGLE_EN = "Count the rows in the February file."


def _child(step, parent=GOAL, steps=None, depth=0):
    kids = fo.child_goals(parent, steps or [step, "別の担当範囲"], parent_task_id="t", depth=depth)
    return kids[0]["text"]


def _set_depth(monkeypatch, value, merge):
    from relay import fleet_runner as FR
    real = FR._settings_int

    def fake(key, default):
        return value if key == fo.DEPTH_SETTING_KEY else real(key, default)
    monkeypatch.setattr(FR, "_settings_int", fake)
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", merge)
    if not merge:
        # the merge default changed to on (2026-10-08): "merge off" must now be explicit
        real_text = FR._settings_text
        monkeypatch.setattr(FR, "_settings_text", lambda key: "off" if key == fo.HIERARCHICAL_SETTING_KEY
                            else real_text(key))


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _texts():
    child1 = [_child(s) for s in (SINGLE_JA, MULTI_JA, MULTI_EN, SINGLE_EN)]
    return child1 + [GOAL, MULTI_JA, SINGLE_EN, "x" * 700, ""]


#: sha256 of [(decision, reason)] of the depth-blind judge on _texts(), taken from the
#: implementation before this change (origin/main).
GOLDEN = "cb6f6a7e23f70dcaf3f161eb3075884642312039dfa9827023672d9108832a51"


def test_the_default_call_is_exactly_the_old_depth_blind_judge():
    rows = [(v.decision, v.reason) for v in (sp.judge(t) for t in _texts())]
    assert _digest(rows) == GOLDEN


def test_at_effective_depth_one_every_text_judges_as_before(monkeypatch):
    _set_depth(monkeypatch, 1, False)
    for depth in (0, 1):
        rows = [(v.decision, v.reason) for v in (
            sp.judge(t, depth=depth, max_depth=fo.effective_max_depth()) for t in _texts())]
        assert _digest(rows) == GOLDEN, depth


def test_a_configured_depth_two_with_merge_off_is_still_depth_one(monkeypatch):
    _set_depth(monkeypatch, 2, False)
    assert fo.effective_max_depth() == 1
    rows = [(v.decision, v.reason) for v in (
        sp.judge(t, depth=1, max_depth=fo.effective_max_depth()) for t in _texts())]
    assert _digest(rows) == GOLDEN


def test_a_child_with_independent_parts_is_asked_at_depth_two(monkeypatch):
    _set_depth(monkeypatch, 2, True)
    for step in (MULTI_JA, MULTI_EN):
        v = sp.judge(_child(step), depth=1, max_depth=fo.effective_max_depth())
        assert v.decision == sp.UNCERTAIN, (step, v.reason)
        assert not v.signals.get("is_child")


def test_a_child_with_one_small_task_is_not_asked(monkeypatch):
    _set_depth(monkeypatch, 2, True)
    for step in (SINGLE_JA, SINGLE_EN):
        v = sp.judge(_child(step), depth=1, max_depth=fo.effective_max_depth())
        assert v.decision == sp.NO_SPLIT, (step, v.reason)


def test_the_parent_goal_and_contract_do_not_make_a_child_look_long(monkeypatch):
    _set_depth(monkeypatch, 2, True)
    big_parent = GOAL + ("補足説明。" * 200)
    text = _child(SINGLE_JA, parent=big_parent)
    assert len(text) > 600
    assert sp.judge(text, depth=1, max_depth=2).decision == sp.NO_SPLIT
    # and a parent that enumerates does not make a single-slice child look enumerated
    parent = GOAL + "\n" + MULTI_JA
    assert sp.judge(_child(SINGLE_JA, parent=parent), depth=1, max_depth=2).decision == sp.NO_SPLIT


def test_a_grandchild_at_the_depth_limit_never_splits(monkeypatch):
    _set_depth(monkeypatch, 2, True)
    top = _child(MULTI_JA)
    grand = fo.child_goals(top, [MULTI_JA, "別"], parent_task_id="t", depth=1)[0]["text"]
    v = sp.judge(grand, depth=2, max_depth=fo.effective_max_depth())
    assert v.decision == sp.NO_SPLIT and v.signals.get("is_child") is True


def test_a_root_goal_is_judged_the_same_at_any_depth_setting():
    for t in (GOAL, MULTI_JA, SINGLE_EN):
        assert sp.judge(t, depth=0, max_depth=2).decision == sp.judge(t).decision


def test_a_marker_without_a_scope_block_stays_a_leaf():
    v = sp.judge("担当範囲を完了したら DONE と書いてください。", depth=1, max_depth=2)
    assert v.decision == sp.NO_SPLIT and v.signals.get("is_child") is True


# ------------------------------------------------------------- the worker is actually asked
def _worker(text, monkeypatch, depth, role="subtask"):
    return rf.RelayWorker({"text": text, "role": role, "depth": depth}, "w0", fanout=True,
                          spawn_fn=lambda *a, **k: None)


def test_a_depth_one_worker_is_offered_a_split_only_when_depth_two_is_in_force(monkeypatch):
    text = _child(MULTI_JA)
    _set_depth(monkeypatch, 2, False)
    w = _worker(text, monkeypatch, 1)
    assert w.fanout is False and w._fanout_capable is False
    _set_depth(monkeypatch, 2, True)
    w = _worker(text, monkeypatch, 1)
    assert w._fanout_capable is True and w.fanout is True
    assert w._split_reason.startswith("UNCERTAIN")
    small = _worker(_child(SINGLE_JA), monkeypatch, 1)
    assert small.fanout is False and small._split_reason.startswith("NO_SPLIT")


def test_a_grandchild_and_an_aggregator_are_never_offered_a_split(monkeypatch):
    _set_depth(monkeypatch, 2, True)
    text = _child(MULTI_JA)
    assert _worker(text, monkeypatch, 2).fanout is False
    assert _worker(text, monkeypatch, 1, role="aggregator").fanout is False


def test_the_depth_one_worker_of_a_depth_one_run_is_unchanged(monkeypatch):
    _set_depth(monkeypatch, 1, False)
    w = _worker(_child(MULTI_JA), monkeypatch, 1)
    assert w.fanout is False and w._fanout_capable is False


@pytest.mark.parametrize("depth", [0, 1])
def test_a_depth_zero_worker_still_sees_a_child_marker_as_a_leaf(monkeypatch, depth):
    _set_depth(monkeypatch, 1, False)
    w = _worker(_child(MULTI_JA), monkeypatch, 0)
    assert w.fanout is False
