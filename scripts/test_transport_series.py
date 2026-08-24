"""事前登録した系列の設計が、走行の前に検査できる形になっていること。

このファイルが存在する理由: 同じ仮説について1日で3つの判定が出た（p=0.0143 → 0.0238 → 0.21）。
毎回、計器が正直になるたびに小さくなった。どれ1つ事前に宣言されていなかった。
"""
import tempfile
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

#: テストは実機の刻印に依存しない。着弾状態は必ず明示的に渡す。
NEVER = float("inf")


def test_a_run_is_refused_when_nothing_says_the_connector_works():
    assert S.preflight({"tool_age_s": None}, inbound_age=NEVER) != ""
    assert S.preflight({"tool_age_s": S.PROBE_STALE_S + 1}, inbound_age=NEVER) != ""
    assert S.preflight({"tool_age_s": 60, "tool_ok": False, "tool_inbound": False},
                       inbound_age=NEVER) != ""


def test_a_recent_healthy_probe_lets_the_run_proceed():
    assert S.preflight({"tool_age_s": 60, "tool_ok": True, "tool_inbound": True},
                       inbound_age=NEVER) == ""


def test_a_failed_probe_that_DID_reach_the_server_is_not_a_connector_problem():
    """応答が使えなかっただけで呼び出しは届いている。輸送の測定は続けてよい。
    ここを塞ぐと、モデルの気まぐれで一晩が止まる。"""
    assert S.preflight({"tool_age_s": 60, "tool_ok": False, "tool_inbound": True},
                       inbound_age=NEVER) == ""


# ---- 隔離 ---------------------------------------------------------------------------------

CONFIG_CDP = S.CONFIG["cdp_url"]


def _rec(pop="fleet-edge-tree", gain=10.0, mc="3", aborted=False, cdp=None):
    return {"control": {"memory_population": pop}, "memory_gain_mb": gain,
            "max_concurrent": int(mc), "infra": {"aborted": aborted},
            "cdp_url": CONFIG_CDP if cdp is None else cdp}


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


def test_a_fresh_arrival_outranks_a_stale_failing_verdict():
    """記録された判定は最後の『定期』プローブのもので、最大1周期ぶん古い。
    着弾刻印は、どのターンが出した呼び出しであれ「サーバに届いた」を言う。
    復旧直後は刻印が新しく判定はまだ障害を語るので、そこで拒否すると
    もう存在しない問題の記録で系列を止めることになる。

    緩めているのではない。門が拒むのは経路が『未検証』のときで、
    窓の内側の着弾はまさにその検証そのもの。"""
    stale_bad = {"tool_age_s": 60, "tool_ok": False, "tool_inbound": False}
    assert S.preflight(stale_bad, inbound_age=NEVER) != ""
    assert S.preflight(stale_bad, inbound_age=120.0) == ""


def test_an_old_arrival_does_not_rescue_a_bad_verdict():
    """刻印が窓の外なら、それは証拠として古い。"""
    stale_bad = {"tool_age_s": 60, "tool_ok": False, "tool_inbound": False}
    assert S.preflight(stale_bad, inbound_age=S.PROBE_STALE_S + 1) != ""


def test_a_run_in_another_browser_is_quarantined():
    """フリートの Edge には、どの腕のものでもない常駐 Copilot ページがある。
    6腕を通じてその最大変動源は 24〜239MB 揺れ、一方で腕が自分で作るプロセスは
    18〜20MB で一定だった。1つのプロセスの commit を2人の住人で割る統計量は
    存在しないので、系列は同居人のいないブラウザを駆動する。

    どちらの走行も同じ `population` 文字列を持つので、母集団の検査では捕まらない。"""
    assert S.classify(_rec(cdp="http://127.0.0.1:9222")) != ""
    assert S.classify(_rec()) == ""


def test_the_series_hands_that_browser_to_the_campaign():
    """設定に書いてあっても、子プロセスに渡していなければ既定のブラウザで走る。"""
    import inspect
    src = inspect.getsource(S.run_one)
    assert 'env["MCP_FLEET_CDP_URL"] = CONFIG["cdp_url"]' in src


def test_every_run_starts_from_a_rebuilt_browser():
    """タブを閉じてもメモリは戻らない。3回の開閉で +422/+305/+160MB が残り、
    落ち着いた基準は 523→863→1084MB と上がった。20走行ぶんそれをやれば、
    端末が尽きるうえに、各腕が比べられる基準そのものが毎回変わる。"""
    import inspect
    src = inspect.getsource(S.run_one)
    assert "rebuild_browser()" in src
    i, j = src.index("rebuild_browser()"), src.index("subprocess.run")
    assert i < j, "測定を始めてから建て直している"


def test_a_failed_rebuild_refuses_rather_than_measures():
    """状態の分からないブラウザで測るくらいなら、測らないほうがよい。"""
    import inspect
    src = inspect.getsource(S.run_one)
    assert '"refused": "browser rebuild:' in src


def test_the_rebuild_is_between_runs_not_between_arms():
    """腕の間で建て直すと、第2腕だけレンダラープールが冷える。
    それは腕ごとの温めが取り除いた非対称そのもの。"""
    import inspect
    assert "rebuild_browser" not in inspect.getsource(S.argv_for)
    doc = inspect.getdoc(S.rebuild_browser) or ""
    assert "not between arms" in doc.lower() or "not between arms" in doc


def test_a_launcher_that_refuses_stops_the_run_instead_of_measuring():
    """起動器が「見えうる窓がある」で拒否コードを返したら、走行は始まってはいけない。

    運用者から「タブを見せるな、次は無い」と言われている。起動器に検査を足しても、
    呼ぶ側がその戻り値を無視すれば意味が無い -- 実際、無視して進む実装は
    「窓が前に出たまま20分測る」形になる。だから拒否は測定不能として扱う。"""
    import scripts.run_transport_series as ts

    calls = []

    def fake_rebuild():
        calls.append(1)
        return "rebuild exited 2: REFUSING: this profile has a window the operator could see"

    def exploded(*a, **k):                              # 走ったら失敗
        raise AssertionError("拒否されたのにキャンペーンを起動した")

    old_rebuild, old_run = ts.rebuild_browser, ts.subprocess.run
    ts.rebuild_browser, ts.subprocess.run = fake_rebuild, exploded
    try:
        out = ts.run_one("sock", "ctrl", tempfile.mkdtemp())
    finally:
        ts.rebuild_browser, ts.subprocess.run = old_rebuild, old_run

    assert calls == [1]
    assert "refused" in out, "拒否を測定結果として通している"
    assert "REFUSING" in out["refused"], "何を拒否したのかが結果に残っていない"


def test_two_treatments_cannot_end_the_series():
    """正規分位点を使っていたせいで、系列が4走行目で止まり CONFIRMED-NULL を印字した。

    treatment 2本 (133.9, 208.3) の標準誤差は 37.2。1.96 倍なら半幅 72.9、上端 244.0 で
    300MB の床を下回り、これは事前登録上「候補は勝てない」の signature。だが自由度1の
    正しい乗数は 12.71 で、半幅 472.7、上端 643.8 -- 何も示されていなかった。

    正規分位点は「ばらつきが既知」を仮定する。それを、今まさにそのばらつきを推定するのに
    使った2点に当てるのは、近似ですらなく6倍外れており、しかも外れ方が
    「自信を捏造する」向き。"""
    import scripts.run_transport_series as ts

    m, half = ts.mean_ci([133.9, 208.3])
    assert round(m, 1) == 171.1
    assert half > 400, "2点の区間が狭すぎる -- 正規分位点に戻っている"

    out = ts.verdict([-127.1, -36.5], [133.9, 208.3])
    assert out["state"] != "CONFIRMED-NULL", "2本で結論を出している"
    assert out["state"] == "collecting"


def test_the_interval_widens_as_the_sample_shrinks():
    """乗数が標本数に応じて動くこと。固定値に戻れば、少数走行での停止がまた起きる。"""
    import scripts.run_transport_series as ts

    assert ts.t95(1) > ts.t95(4) > ts.t95(10) >= 1.96
    wide = ts.mean_ci([100.0, 200.0])[1]
    narrow = ts.mean_ci([100.0, 200.0] * 6)[1]
    assert wide > narrow * 4, "標本を増やしても区間が縮んでいない"
