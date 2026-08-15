"""The harness tree: does it stay accountable once there is more than one harness?

Every guarantee built in the earlier phases is a statement about a SINGLE harness. A tree
turns each into a fact-per-class, and does it silently -- everything still runs, and "which
harness produced this number" quietly stops having an answer. These tests are about keeping
that answer.
"""
from __future__ import annotations

import pytest

from relay.selfimprove import harness_tree as T
from relay.selfimprove import manifest as M


def _tree(**overrides):
    return {"root": M.base_manifest(), "overrides": overrides}


def test_a_class_with_no_branch_runs_the_reviewed_configuration():
    """新しい種類の仕事は絶えず現れる。落ちる先は、人が見た設定でなければならない。"""
    got = T.resolve(_tree(), "something_nobody_planned_for")
    assert got["matched"] is None
    assert got["harness_id"] == M.harness_id(M.base_manifest())


def test_a_branch_applies_and_says_what_it_changed():
    got = T.resolve(_tree(long_running={"parameters": {"max_retries": 20}}), "long_running")
    assert got["matched"] == "long_running"
    assert got["overrides"] == {"parameters.max_retries": (10, 20)}
    assert got["harness_id"] != M.harness_id(M.base_manifest())


def test_every_resolution_is_accountable():
    """説明できない設定は再現できない設定。"""
    got = T.resolve(_tree(excel={"parameters": {"memory_max_items": 2}}), "excel")
    for key in ("manifest", "matched", "overrides", "harness_id"):
        assert key in got


def test_a_branch_cannot_introduce_something_the_root_does_not_declare():
    """枝が許可リストの見ていないコンポーネントを持ち込めるなら、
    許可リストは根にしか掛かっていない。"""
    with pytest.raises(T.TreeError) as exc:
        T.resolve({"root": M.base_manifest(),
                   "overrides": {"x": {"components": {"planner": "planner/v2"}}}}, "x")
    assert "does not declare" in str(exc.value)


def test_a_branch_that_would_be_an_illegal_manifest_is_refused():
    """個々には無害でも、合成した結果が不正なら走るのはその合成結果。"""
    with pytest.raises(Exception):
        T.resolve(_tree(x={"parameters": {"max_retries": 10_000}}), "x")


def test_a_tree_without_a_root_is_refused():
    with pytest.raises(T.TreeError) as exc:
        T.validate({"overrides": {}})
    assert "reviewed configuration to fall back to" in str(exc.value)


def test_branches_that_resolve_to_the_same_harness_are_reported_as_one():
    """5つの枝が3つの manifest に落ちるなら、2つは設定ではなく文書。"""
    tree = _tree(a={"parameters": {"memory_max_items": 9}},
                 b={"parameters": {"memory_max_items": 9}},
                 c={})
    got = T.branches(tree)
    ids = [b["harness_id"] for b in got]
    assert len(ids) == len(set(ids))
    same = [b for b in got if len(b["classes"]) > 1]
    assert same, "同一 manifest に落ちる枝がまとめられていない"


def test_a_branch_nobody_measured_is_named_as_unjustified():
    """測っていない枝は、そのクラスに偶然含まれていたノイズに合わせた設定。"""
    tree = _tree(measured={"parameters": {"memory_max_items": 9}},
                 guessed={"parameters": {"memory_max_items": 2}})
    out = T.justified(tree, {"measured": ["INCONCLUSIVE", "KEEP"],
                             "guessed": ["INCONCLUSIVE"]})
    assert out["justified"] == ["measured"]
    assert out["unjustified"] == ["guessed"]


def test_the_tree_does_not_grow_itself():
    """どのクラスが独自ハーネスに値するかは campaign が答える問い。
    自動で枝を生やすと、ノイズに合わせた per-class 設定が量産される。"""
    import inspect
    src = inspect.getsource(T)
    for verb in ("def grow", "def learn", "overrides[task_class] ="):
        assert verb not in src, "木が自分で枝を作っている: %s" % verb
