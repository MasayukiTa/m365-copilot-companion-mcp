"""事前登録した系列の設計が、走行の前に検査できる形になっていること。

このファイルが存在する理由: 同じ仮説について1日で3つの判定が出た（p=0.0143 → 0.0238 → 0.21）。
毎回、計器が正直になるたびに小さくなった。どれ1つ事前に宣言されていなかった。
"""
import ast
import inspect
import collections

import pytest

from scripts import run_transport_series as S


# ---- 設計 ---------------------------------------------------------------------------------

def test_the_orders_are_balanced():
    """順序効果は実在する（旧計器の帰無では後攻の腕が両方とも高い山を持った）。
    片側に偏れば、その効果が処置列に漏れる。"""
    orders = collections.Counter(o for _k, o in S.PHASE_A + S.PHASE_B)
    assert orders["ctrl"] == orders["cand"], orders


def test_both_null_flavours_are_present_in_equal_number():
    """socket 条件だけで帰無を取ったことが、生き残らなかった p=0.0143 の正体。
    処置の腕はタブと socket の両方を含むので、対照も両方を含む必要がある。"""
    kinds = collections.Counter(k for k, _o in S.PHASE_A + S.PHASE_B)
    assert kinds["sock"] == kinds["tabs"] > 0, kinds
    assert kinds["tx"] == 8, kinds


def test_phase_a_covers_every_flavour_by_order_cell_twice():
    """1セル1本だと、順序の効果と種別の効果と偶然が区別できない。"""
    cells = collections.Counter(S.PHASE_A)
    assert set(cells) == {("sock", "ctrl"), ("sock", "cand"),
                          ("tabs", "ctrl"), ("tabs", "cand")}
    assert set(cells.values()) == {2}, cells


def test_treatments_are_interleaved_with_nulls():
    """処置を固めて走らせると、夜間のドリフトが処置列だけに乗る。"""
    kinds = [k for k, _o in S.PHASE_B]
    assert "sock" in kinds[:6] or "tabs" in kinds[:6], kinds


@pytest.mark.parametrize("kind,order,expected", [
    ("sock", "ctrl", ["--warmup", "--null", "--socket-both"]),
    ("sock", "cand", ["--warmup", "--null", "--socket-both", "--candidate-first"]),
    ("tabs", "ctrl", ["--warmup", "--null"]),
    ("tx", "ctrl", ["--warmup"]),
    ("tx", "cand", ["--warmup", "--candidate-first"]),
])
def test_each_cell_maps_to_the_right_flags(kind, order, expected):
    assert S.argv_for(kind, order) == expected


def test_the_floor_is_taken_from_the_frozen_judge_not_restated(monkeypatch):
    """床をここに書き写すと、片方だけ動かせてしまう。判定器が正本。

    ソース中の数値リテラルを探す版を先に書いたが、`time.sleep(300)` に当たって
    落ちた。待ち時間と閾値を区別できない検査は、いずれ「通らないから」と
    緩められる。だから挙動で見る -- 判定器の床を動かせば判定が動くこと。"""
    from relay.selfimprove import route_evaluator as RE
    assert S.floor_mb() == RE.MIN_MEMORY_GAIN_MB

    nulls = [10, 20, 15, 25, 12, 18, 22, 14]
    txs = [500, 520, 480, 510]
    assert S.verdict(nulls, txs)["state"] == "CONFIRMED"
    monkeypatch.setattr(RE, "MIN_MEMORY_GAIN_MB", 5000.0)
    again = S.verdict(nulls, txs)
    assert again["floor_mb"] == 5000.0
    assert again["state"] != "CONFIRMED", "床を動かしても判定が変わらない＝床を見ていない"


# ---- 走行前検査 ---------------------------------------------------------------------------

def test_a_run_is_refused_when_nothing_says_the_connector_works():
    assert S.preflight({"tool_age_s": None}) != ""
    assert S.preflight({"tool_age_s": S.PROBE_STALE_S + 1}) != ""
    assert S.preflight({"tool_age_s": 60, "tool_ok": False, "tool_inbound": False}) != ""


def test_a_recent_healthy_probe_lets_the_run_proceed():
    assert S.preflight({"tool_age_s": 60, "tool_ok": True, "tool_inbound": True}) == ""


def test_a_failed_probe_that_DID_reach_the_server_is_not_a_connector_problem():
    """応答が使えなかっただけで呼び出しは届いている。輸送の測定は続けてよい。
    ここを塞ぐと、モデルの気まぐれで一晩が止まる。"""
    assert S.preflight({"tool_age_s": 60, "tool_ok": False, "tool_inbound": True}) == ""


# ---- 隔離 ---------------------------------------------------------------------------------

def _rec(pop="fleet-edge-tree", gain=10.0, mc="3", aborted=False):
    return {"control": {"memory_population": pop}, "memory_gain_mb": gain,
            "max_concurrent": int(mc), "infra": {"aborted": aborted}}


def test_an_unscoped_run_is_quarantined_not_averaged():
    """全 Edge を数えた走行は別の量。実測でその母集団の 59% は無関係だった。"""
    assert S.classify(_rec(pop="all-edge-unscoped")) != ""
    assert S.classify(_rec()) == ""


def test_a_run_at_another_concurrency_is_quarantined():
    """並列度は測定の一部。速い設定は、同じものを早く得たのではなく別のものを測っている。"""
    assert S.classify(_rec(mc="2")) != ""


def test_an_infra_abort_is_not_a_measurement():
    assert S.classify(_rec(aborted=True)) != ""
    assert S.classify(_rec(gain=None)) != ""


# ---- 判定 ---------------------------------------------------------------------------------

def test_a_clear_win_reads_as_confirmed():
    v = S.verdict([10, 20, 15, 25, 12, 18, 22, 14], [500, 520, 480, 510])
    assert v["state"] == "CONFIRMED"


def test_an_interval_below_the_floor_is_an_answer_not_a_failure():
    v = S.verdict([10, 20, 15, 25, 12, 18, 22, 14], [40, 55, 30, 45, 38, 50])
    assert v["state"] == "CONFIRMED-NULL"


def test_straddling_the_floor_keeps_collecting_rather_than_guessing():
    v = S.verdict([10, 20, 15, 25], [250, 350, 300, 320])
    assert v["state"] == "collecting"


def test_one_run_over_the_floor_decides_nothing():
    """旧計器では帰無が +196.8MB に達した。1本が床を超えるのはコイン投げで、
    床はまさにそれを拒むために在る。"""
    v = S.verdict([10, 20], [400])
    assert v["state"] == "collecting"


def test_a_null_as_large_as_the_treatment_mean_blocks_confirmation():
    """完全分離が条件。処置平均に届く帰無が1本でもあれば、それは分離ではない。"""
    v = S.verdict([10, 20, 15, 505, 12, 18, 22, 14], [500, 520, 480, 510])
    assert v["state"] != "CONFIRMED"
