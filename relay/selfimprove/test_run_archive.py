"""どの走行を同じ列に並べてよいかの規則。手作業で一度やって二度とも間違えた判断。"""
import json
import os

import pytest

from relay.selfimprove import run_archive as RA


def _write(d, exp, *, null, gain, goals=None, control=200.0, candidate=150.0,
           population="fleet-edge-tree"):
    rec = {"ledger_experiment_id": exp, "null_run": null, "memory_gain_mb": gain,
           "arm_order": "control,candidate",
           "control": {"peak_mb": control, "memory_population": population},
           "candidate": {"peak_mb": candidate, "memory_population": population}}
    if goals is not None:
        rec["goals"] = goals
    with open(os.path.join(d, "route_campaign_%s.json" % exp), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


@pytest.fixture
def archive(tmp_path):
    d = str(tmp_path)
    # 飽和集合にも世代がある(短縮パス修正)。計器の時刻だけを基準にすると、
    # 作業負荷の世代で落ちたのをテストの不具合と読み違える。
    base = max(RA.INSTRUMENT_EPOCH, RA.WORKLOAD_EPOCH.get("saturated-v1", 0))
    before = RA.SAMPLER_EPOCH - 100
    after = base + 100
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % before, null=True, gain=-179.8)
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % after, null=True, gain=50.8)
    _write(d, "route-transport-v1-ctrlfirst-%d" % (after + 10), null=False, gain=76.7)
    return d


def test_a_run_from_before_the_instrument_change_is_not_older_data_but_other_data(archive):
    """全RSS計測は「どちらの腕が先に走ったか」を測っていた。
    古いから軽く見る、ではなく別の量なので、平均に混ぜるのは保守的ですらない。"""
    runs = RA.load(archive)
    assert len(runs) == 3
    assert [r["current_instrument"] for r in runs] == [False, True, True]
    nulls = RA.comparable(runs, goals="saturated-v1", null=True)
    assert [r["memory_gain_mb"] for r in nulls] == [50.8]


def test_a_superseded_goal_set_keeps_its_name(tmp_path, monkeypatch):
    """欠陥のあった `multiturn` の走行と、直した後の走行は同じ `goals` 文字列を持つ。
    名前だけで絞ると、壊れたゴールを測った数値が黙って平均に入る。

    世代は計測器のほうより必ず後ろに置く。そうしないと、作業負荷の世代を試したつもりで
    計測器の世代に弾かれ、通る理由が入れ替わったことに気づけない。"""
    d = str(tmp_path)
    epoch = RA.INSTRUMENT_EPOCH + 1000
    monkeypatch.setattr(RA, "WORKLOAD_EPOCH", {"multiturn": epoch})
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (epoch - 100),
           null=True, gain=132.7, goals="multiturn")
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (epoch + 100),
           null=True, gain=-5.3, goals="multiturn")
    runs = RA.load(d)
    assert len(runs) == 2
    assert all(r["current_instrument"] for r in runs), "計測器の世代で弾かれている"
    kept = RA.comparable(runs, goals="multiturn", null=True)
    assert [r["memory_gain_mb"] for r in kept] == [-5.3]


def test_two_goal_sets_are_never_put_in_one_column(tmp_path, monkeypatch):
    """片方の集合で測った差は、もう片方についての証拠ではない。広がりが違う。"""
    d = str(tmp_path)
    after = RA.INSTRUMENT_EPOCH + 2000
    monkeypatch.setattr(RA, "WORKLOAD_EPOCH", {})
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % after, null=True, gain=50.8)
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (after + 5),
           null=True, gain=196.8, goals="multiturn")
    runs = RA.load(d)
    sat = RA.comparable(runs, goals="saturated-v1", null=True)
    mt = RA.comparable(runs, goals="multiturn", null=True)
    assert [r["memory_gain_mb"] for r in sat] == [50.8]
    assert [r["memory_gain_mb"] for r in mt] == [196.8]


def test_the_summary_reports_nothing_rather_than_zero_when_there_is_nothing(tmp_path):
    """空の列を 0 と報告すると、測っていないことが『差が無い』に化ける。"""
    s = RA.spread([])
    assert s["n"] == 0
    assert s["mean"] is None and s["widest"] is None


def test_it_does_not_issue_a_verdict():
    """判定は凍結された `route_evaluator.decide` の仕事。
    ここが二つ目の判定器になると、床を動かす行為が台帳を通らずに起きる。"""
    import inspect
    src = inspect.getsource(RA)
    for word in ("keep", "verdict", "reject"):
        assert ("return {\"%s\"" % word) not in src, word
    assert not hasattr(RA, "decide")


def test_each_change_that_altered_the_quantity_pushes_the_epoch_forward():
    """3つ目の変更(アドミッションの重み付け)は、記録された `max_concurrent` が
    両側で同じ値のまま中身だけ変わったもの。記録項目では捕まえられないので、
    世代で切るしかない。"""
    assert RA.INSTRUMENT_EPOCH > RA.SAMPLER_ISOLATION_EPOCH > RA.SAMPLER_EPOCH


# ---- 分離の度合い -------------------------------------------------------------------------------

def test_a_clean_separation_at_two_against_two_cannot_beat_one_in_six():
    """完全に分離していても p は 1/6 で止まる。
    その p は効果の強さではなく標本の大きさを語っている。"""
    s = RA.separation([-1.3, 50.8], [76.7, 188.8])
    assert s["p"] == s["min_p"] == pytest.approx(0.1667, abs=1e-3)
    assert s["observed"] == pytest.approx(108.0, abs=0.1)


def test_more_runs_lower_the_floor():
    """4対4なら 1/70。あと4本走らせる意味はここにある。"""
    assert RA.separation([1, 2], [3, 4])["min_p"] == pytest.approx(1 / 6, abs=1e-4)
    assert RA.separation([1, 2, 3, 4], [5, 6, 7, 8])["min_p"] == pytest.approx(1 / 70, abs=1e-4)


def test_the_test_is_one_sided_in_the_direction_declared_in_advance():
    """向きは仮説として先に決めてある(socket は使用量が減る)。
    見てから向きを決めれば p はただで半分になる。引数を入れ替えれば結論も逆になること。"""
    forward = RA.separation([-1.3, 50.8], [76.7, 188.8])["p"]
    backward = RA.separation([76.7, 188.8], [-1.3, 50.8])["p"]
    assert forward < backward
    assert backward == pytest.approx(1.0, abs=1e-6)


def test_overlapping_columns_are_not_reported_as_separated():
    s = RA.separation([10, 200], [20, 150])
    assert s["p"] > s["min_p"]


def test_an_empty_column_reports_nothing_and_says_why():
    """空を p=1 と返すと『差が無い』に化ける。測っていないことは差が無いことではない。"""
    s = RA.separation([], [76.7])
    assert s["p"] is None and s["why"]


def test_a_run_that_counted_every_browser_is_not_the_same_measurement(tmp_path, monkeypatch):
    """サンプラは CDP ポートの持ち主を解決できないと全 Edge 合計に戻り、
    そのことを結果に書く。書くだけでは足りない -- 誰もそれで絞らないなら、
    同じ列に並んでしまう。実測でその母集団の 59% は無関係だった。"""
    d = str(tmp_path)
    monkeypatch.setattr(RA, "WORKLOAD_EPOCH", {})
    t = RA.INSTRUMENT_EPOCH + 3000
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % t, null=True, gain=10.0,
           population="fleet-edge-tree")
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (t + 5), null=True, gain=960.0,
           population="all-edge-unscoped")
    runs = RA.load(d)
    scoped = RA.comparable(runs, goals="saturated-v1", null=True)
    assert [r["memory_gain_mb"] for r in scoped] == [10.0]
    unscoped = RA.comparable(runs, goals="saturated-v1", null=True,
                             memory_population="all-edge-unscoped")
    assert [r["memory_gain_mb"] for r in unscoped] == [960.0]


def test_a_change_to_the_browser_being_measured_moves_the_epoch():
    """ブラウザの起動形態を変えたのに世代を上げなかった -- 8番目の欠陥。

    評価用ブラウザは窓付きで起動し、イントラのポータル(273MB)を開始ページにしていた。
    headless 化でその両方が消えたが、これはサンプラの測定対象そのものの変更であり、
    再スコープと同じ種類の変更。なのに世代は据え置かれ、窓あり4本と headless 1本が
    「同じ計器」として同じ籠に入っていた。

    このテストは番号を丸暗記させるためではなく、世代が「窓あり時代の最後の走行」より
    後を指していることを見るためのもの。前に戻せば、混ぜてはいけないものが再び混ざる。"""
    # 窓あり時代の最後の走行 (19:04)。これが現行計器に含まれてはいけない。
    LAST_WINDOWED_RUN = 1787565886
    assert RA.INSTRUMENT_EPOCH > LAST_WINDOWED_RUN, (
        "世代が窓あり時代を含んでいる -- 別のブラウザの測定を混ぜることになる")


def test_unwarmed_runs_cannot_share_a_column_with_warmed_ones():
    """warm-up の有無は「どう走らせたか」ではなく「何を測ったか」の一部。

    warm-up は毎アーム前に tabs 経路を走らせるので、各アームの基準線には既に
    レンダラが1個入っている -- tabs アームはそれを無料で再利用し、socket アームは
    減衰させる。二人のレビュアーがこのバイアスの向きを正反対に読み、決着させる診断は
    warm-up 無しで走る。その結果は同じアーカイブに落ちる。

    同じ晩に、窓ありの走行4本が headless の1本と同じ籠に入っていた。同じ性質の欠陥を、
    今度は起きた後ではなく起きる前に塞ぐ。"""
    warmed = {"warmup": True, "current_instrument": True, "goals": "saturated-v1",
              "version": "v1", "ts": 10 ** 12, "max_concurrent": 3, "sidepage_reserve": "1",
              "memory_population": "fleet-edge-tree", "cdp_url": "http://127.0.0.1:9224",
              "null": True, "memory_gain_mb": 10.0}
    cold = dict(warmed, warmup=False, memory_gain_mb=999.0)

    kw = dict(goals="saturated-v1", max_concurrent=3, cdp_url="http://127.0.0.1:9224")
    got = RA.comparable([warmed, cold], **kw)                    # 既定は warmup=True
    assert [r["memory_gain_mb"] for r in got] == [10.0], "warm-up 無しが混ざっている"

    got = RA.comparable([warmed, cold], warmup=False, **kw)
    assert [r["memory_gain_mb"] for r in got] == [999.0]


def test_a_run_recorded_before_the_flag_existed_counts_as_warmed():
    """記録が始まる前の走行は全て --warmup 付きで走っていた。
    既定を False にすると、系列全体が診断側に落ちて静かに消える。"""
    assert RA.comparable(
        [{"current_instrument": True, "goals": "saturated-v1", "version": "v1",
          "ts": 10 ** 12, "max_concurrent": 3, "sidepage_reserve": "1",
          "memory_population": "fleet-edge-tree", "cdp_url": "http://127.0.0.1:9224",
          "null": True, "memory_gain_mb": 1.0, "warmup": True}],
        goals="saturated-v1", max_concurrent=3, cdp_url="http://127.0.0.1:9224")
