"""Tests for the warm-up bias diagnostic."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "scripts" / "diag_warmup_bias.py").read_text(encoding="utf-8")


def test_the_diagnostic_never_warms_up():
    """warm-up 無しで走ることが、この診断の存在理由そのもの。
    うっかり付ければ、二人が割れた当の条件を測れなくなる。"""
    # コメントで --warmup に言及するのは正しい(なぜ付けないかを書いてある)。
    # 見るべきは、実際にキャンペーンへ渡す引数にそれが混ざっていないか。
    code = [ln for ln in SRC.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    offenders = [ln.strip() for ln in code if '"--warmup"' in ln or "'--warmup'" in ln]
    assert not offenders, "診断が warm-up を渡している: %s" % offenders
    assert '["--null"]' in SRC, "null 走行(両アーム同一経路)になっていない"


def test_it_rebuilds_before_measuring_and_refuses_if_it_cannot():
    """1本目のアームが冷えていることが前提。前の走行の残骸が残っていたら冷えていない。"""
    i, j = SRC.index("def rebuild"), SRC.index("def main")
    assert "start_eval_edge.ps1" in SRC[i:j]
    assert "REFUSED" in SRC[j:], "再構築に失敗しても測りに行っている"


def test_the_idle_period_does_no_work():
    """sham は『何もしない』ことが測定内容。走らせる物が1つでもあれば sham ではない。"""
    i = SRC.index("if a.idle_s > 0:")
    block = SRC[i:i + 500]
    for banned in ("subprocess", "run_route_campaign", "Popen"):
        assert banned not in block, "無操作区間で %s を動かしている" % banned
    assert "time.sleep(a.idle_s)" in block


def test_it_records_when_each_thing_happened():
    """絶対値の痕跡は、区間を切り出せて初めて読める。
    アーム境界を後から推定する羽目になると、推定の誤差が結論に化ける。"""
    for name in ("browser_rebuilt", "settled_fresh", "run_start", "run_end",
                 "idle_start", "idle_end"):
        assert '"%s"' % name in SRC or "'%s'" % name in SRC or name in SRC, name
    assert "_events.json" in SRC


def test_it_measures_absolute_working_set_not_a_delta():
    """争点は全て『基準線に何が入っているか』。その基準線からの差では、基準線を語れない。"""
    assert "watch_tree_ws.py" in SRC


def test_the_concurrency_is_settable_and_recorded():
    """塊としての費用では、ワーカー1人あたりの予約を決められない。

    これまでの測定は全て同時3で走っており、アーム全体の値しか出ない。受け入れ制御が
    訊いているのは『もう1人入れたらいくらか』だけなので、同時実行数を振って傾きを取る。
    そして振った値が結果に残らなければ、後から傾きを引けない -- 今夜すでに、記録されて
    いない条件が別々の走行を1つの籠に入れる事故を2回起こしている。"""
    assert '"--concurrency"' in SRC, "同時実行数を振れない"
    assert 'env["MCP_FLEET_MAX_CONCURRENT"] = a.concurrency' in SRC, "振った値が渡っていない"
    assert '"concurrency": a.concurrency' in SRC, "振った値が結果に残らない"
    # 3 決め打ちが残っていないこと
    assert 'MCP_FLEET_MAX_CONCURRENT"] = "3"' not in SRC
