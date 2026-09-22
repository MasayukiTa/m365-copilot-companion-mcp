# -*- coding: utf-8 -*-
"""到達しなかった段は、「いいえ」と答えた段ではない。

`mechanism_telemetry.record` の docstring が規則を書いている:

    None は「上の段で止まったので、この段には到達しなかった」
    False は「この段に到達して、答えが no だった」
    **この2つを潰すと、切られている機構が「動いて何もしなかった」機構に見える。**

**そして同じモジュールの `funnel` が、全ての段で潰していた。** None は falsy なので
`if r.get("eligible")` が両方を弾く。`stops_at` はその結果を読んで
「no opportunity（ここでは起きない問題を解いている）」と報告する — **記録の穴から、
世界についての主張が作られていた。**

## 実測（2026-09-22、本番の台帳 7,249 行）

`effort` は configured=416 / eligible=0。これを「機会が無い」と読むのは誤りで、
**416行すべてが `eligible=None`** — 起動時に「何が構成されているか」だけを書く経路が
1つあり、その後この段を書く者がいない。`refuter` と `fanout` も同じ経路から同数。
台帳の「ineligible」行の**ちょうど半分**がこれだった。

2つは**逆の作業を呼ぶ**ので、1つの数字にしてはいけない:
機会が無い機構は**畳む**対象、判定されない機構は**計器を仕上げる**対象。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import mechanism_telemetry as MT  # noqa: E402


def _rows(**over):
    base = {"mechanism": "effort", "configured": True}
    base.update(over)
    return [base]


def test_an_unassessed_step_is_not_reported_as_no_opportunity():
    """**欠陥そのもの。** `eligible` を一度も書かなかった機構が、
    「ここでは起きない問題を解いている」と報告されていた。"""
    f = MT.funnel(_rows(eligible=None), "effort")["effort"]
    assert f["eligible"] == 0
    assert f["eligibility_undetermined"] == 1
    assert f["eligibility_said_no"] == 0
    assert f["stops_at"] == "not assessed", f["stops_at"]


def test_a_step_that_was_asked_and_said_no_still_reads_as_no_opportunity():
    """**もう半分を壊さない。** 判定されて no だったものは、これまでどおり。"""
    f = MT.funnel(_rows(eligible=False, ineligible_reason="nothing to refute"),
                  "effort")["effort"]
    assert f["eligibility_said_no"] == 1
    assert f["eligibility_undetermined"] == 0
    assert f["stops_at"] == "no opportunity"


def test_the_two_are_not_added_together():
    """1つの数字にすると、また同じ読み違いが起きる。"""
    rows = [{"mechanism": "effort", "configured": True, "eligible": None},
            {"mechanism": "effort", "configured": True, "eligible": False},
            {"mechanism": "effort", "configured": True, "eligible": True}]
    f = MT.funnel(rows, "effort")["effort"]
    assert (f["eligible"], f["eligibility_said_no"], f["eligibility_undetermined"]) == (1, 1, 1)


def test_a_mechanism_nobody_configured_still_stops_there_first():
    """段は順序を持つ。構成されていないものを「未判定」と言うのは、
    一段先の話にすり替えること。"""
    f = MT.funnel([{"mechanism": "effort", "configured": False}], "effort")["effort"]
    assert f["stops_at"] == "never configured"


def test_the_live_ledger_still_carries_the_case_this_was_written_for():
    """**この検査が守っているものが現実に在ることを確かめる。** 台帳が無い環境では
    黙って通す（CI には `.fleet/` が無い）が、在るなら未判定が残っているはず。

    なお「在るのに0件」になったら、それは**直った**ということなので、そのときは
    この検査を消す番になる — 片方向の表にしないために、ここに書いておく。
    """
    log = os.path.join(REPO, ".fleet", "mechanisms.jsonl")
    if not os.path.isfile(log):
        return
    f = MT.funnel(MT.load(log))
    undet = {m: d["eligibility_undetermined"] for m, d in f.items()
             if d.get("eligibility_undetermined")}
    if not undet:
        return          # fixed at the writer; nothing left for this to describe
    assert all(f[m]["configured"] >= n for m, n in undet.items()), \
        "more rows are undetermined than were configured, which cannot be"
