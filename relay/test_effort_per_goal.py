"""ゴール1件ごとに effort を決められること。

## 何が固定されているか(実測)

`--effort` は `fleet_runner` で1回だけ読まれ、4つの run 単位の値
(refuter / max_refute / max_research / review_lenses)になって `run_relay_fleet` に渡る。
そこから全ワーカーが同じ4つを受け取る。ゴール側にこれを表現する場所は無く、
`goal_fields` はゴールを `(text, checks, cwd)` にしか正規化しない。

結果、20ゴールのうち1本が難しく19本が算術のような走行では、
**難しい1本の値段を20回払うか、必要だった1本に安い値段を払うか**の二択しかない。

## 一律であることの費用は、リポジトリ自身が記録している

`fleet_runner` のコメント:

> a UNIFORM ultra over-engineers easy tasks
> (observed: 44-47 line diffs for 2-7 line gold fixes)

これは**ゴール単位 effort を求める観察**であり、当時の答えは「4つ目の一律モード(auto)を足す」
だった。モードを増やしても一律であることは変わらない。

## 直交性は壊さない

effort(どれだけ働くか)とタスク種別(何を見るか)を直交させる原則は `refuter.py` に明記があり、
ここでは触らない。レンズの選択規則は従来どおりで、変えるのは**その規則を run 全体に適用するか
ゴールごとに適用するか**だけ。
"""
import pytest

from relay.effort import KNOBS, LEVELS, describe, goal_effort, resolve

RUN_AUTO = {"refuter": True, "max_refute": 3, "max_research": 3, "review_lenses": None}


# ---- ゴールが何も言わなければ、走行の既定のまま --------------------------------------------

def test_a_plain_string_goal_takes_the_run_default():
    """今日のゴールは全部これ。既定の挙動が1ミリも変わらないこと。"""
    assert resolve("算術の問題", RUN_AUTO) == RUN_AUTO


def test_a_dict_goal_without_an_effort_takes_the_run_default():
    assert resolve({"text": "算術の問題", "checks": []}, RUN_AUTO) == RUN_AUTO


def test_none_and_garbage_do_not_crash():
    assert resolve(None, RUN_AUTO) == RUN_AUTO
    assert resolve({"effort": None}, RUN_AUTO) == RUN_AUTO
    assert resolve({"effort": "   "}, RUN_AUTO) == RUN_AUTO


# ---- ゴールが言えば、そのゴールだけが変わる ------------------------------------------------

def test_a_goal_may_ask_for_less():
    """安い1本だけを min に落とせること。走行全体を落とさずに。"""
    got = resolve({"text": "147と288を足して", "effort": "min"}, RUN_AUTO)
    assert got["refuter"] is False
    assert got["max_refute"] == 0
    assert got["max_research"] == 0


def test_a_goal_may_ask_for_more():
    """難しい1本だけを ultra に上げられること。19本を巻き込まずに。"""
    got = resolve({"text": "この障害の根本原因を特定して", "effort": "ultra"}, RUN_AUTO)
    assert got["refuter"] is True
    assert got["max_refute"] == 4
    assert got["review_lenses"] == ["correctness", "edge", "security"]


def test_the_run_default_is_not_mutated():
    """1件解決したら次の1件に漏れる、が起きないこと。"""
    before = dict(RUN_AUTO)
    resolve({"text": "x", "effort": "ultra"}, RUN_AUTO)
    resolve({"text": "y", "effort": "min"}, RUN_AUTO)
    assert RUN_AUTO == before


def test_the_panel_list_is_a_copy_per_goal():
    """レンズ表を共有すると、1ワーカーが append した瞬間に全員に効く。"""
    a = resolve({"text": "x", "effort": "ultra"}, RUN_AUTO)
    b = resolve({"text": "y", "effort": "ultra"}, RUN_AUTO)
    assert a["review_lenses"] == b["review_lenses"]
    assert a["review_lenses"] is not b["review_lenses"]
    assert a["review_lenses"] is not LEVELS["ultra"]["review_lenses"]


# ---- 機械が書くゴールは metadata に置く ----------------------------------------------------

def test_metadata_carries_it_for_machinery():
    """fan-out の子や再試行は envelope を組み立てるので、そちらの置き場も要る。"""
    assert goal_effort({"metadata": {"effort": "ultra"}}) == "ultra"


def test_a_top_level_effort_wins_over_metadata():
    """人が書いた値が、機械が付けた値より強いこと。上書きの向きを固定する。"""
    got = goal_effort({"effort": "min", "metadata": {"effort": "ultra"}})
    assert got == "min"


def test_the_level_name_is_case_and_space_insensitive():
    assert goal_effort({"effort": " ULTRA "}) == "ultra"


# ---- 知らない名前は、黙って既定に落ちない ---------------------------------------------------

def test_an_unknown_level_falls_back_and_says_so():
    """`ultra2` のような打ち間違いを黙って既定にすると、
    ゴールファイルは『最大を要求している』ように読めるのに、そのゴールは審査を失う。

    既定に落ちること自体は正しい —— ゴールごと拒否するほうが悪い —— が、
    **理解できなかった名前は必ず言う**。
    """
    said = []
    got = resolve({"text": "x", "effort": "ultra2"}, RUN_AUTO, log=said.append)
    assert got == RUN_AUTO
    assert said and "ultra2" in said[0], "理解できなかった名前を報告していない"


# ---- 4つの摘みが両側で揃っていること ---------------------------------------------------------

def test_every_level_sets_every_knob():
    """片方の経路にだけ5つ目の摘みが増える、を防ぐ。

    コックピットと Python が『どの outcome が再試行対象か』で食い違った実例があるので、
    同じ形をここで作らない。
    """
    for name, level in LEVELS.items():
        assert set(level) == set(KNOBS), "%s の摘みが揃っていない: %r" % (name, sorted(level))


def test_the_levels_match_the_runner_branches():
    """`fleet_runner` の分岐と数値が一致していること。ずれれば同じ名前が別物になる。"""
    import io
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with io.open(os.path.join(root, "relay", "fleet_runner.py"), encoding="utf-8") as fh:
        src = fh.read()
    blk = src[src.index('if _eff == "min":'):src.index('if args.panel and')]
    code = "\n".join(l for l in blk.splitlines() if not l.strip().startswith("#"))

    # min は審査なし
    assert re.search(r'_eff == "min":\s*\n\s*args\.refuter = False', code)
    # ultra はパネル3枚
    assert "PANEL_LENSES" in code
    for name in ("min", "max", "ultra", "auto"):
        assert '_eff == "%s"' % name in code, "%s の分岐が無い" % name


def test_describe_names_what_a_worker_was_given():
    line = describe(resolve({"text": "x", "effort": "ultra"}, RUN_AUTO))
    assert "refuter=True" in line and "correctness" in line
