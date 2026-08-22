"""どの走行を同じ列に並べてよいかの規則。手作業で一度やって二度とも間違えた判断。"""
import json
import os

import pytest

from relay.selfimprove import run_archive as RA


def _write(d, exp, *, null, gain, goals=None, control=200.0, candidate=150.0):
    rec = {"ledger_experiment_id": exp, "null_run": null, "memory_gain_mb": gain,
           "arm_order": "control,candidate",
           "control": {"peak_mb": control}, "candidate": {"peak_mb": candidate}}
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
    before = RA.INSTRUMENT_EPOCH - 100
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


def test_a_superseded_goal_set_keeps_its_name(tmp_path):
    """欠陥のあった `multiturn` の走行と、直した後の走行は同じ `goals` 文字列を持つ。
    名前だけで絞ると、壊れたゴールを測った数値が黙って平均に入る。"""
    d = str(tmp_path)
    epoch = RA.WORKLOAD_EPOCH["multiturn"]
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (epoch - 100),
           null=True, gain=132.7, goals="multiturn")
    _write(d, "route-transport-v1-NULL-ctrlfirst-%d" % (epoch + 100),
           null=True, gain=-5.3, goals="multiturn")
    runs = RA.load(d)
    assert len(runs) == 2
    kept = RA.comparable(runs, goals="multiturn", null=True)
    assert [r["memory_gain_mb"] for r in kept] == [-5.3]


def test_two_goal_sets_are_never_put_in_one_column(archive):
    """片方の集合で測った差は、もう片方についての証拠ではない。広がりが違う。"""
    _write(archive, "route-transport-v1-NULL-ctrlfirst-%d"
           % (max(RA.WORKLOAD_EPOCH.values()) + 5), null=True, gain=196.8, goals="multiturn")
    runs = RA.load(archive)
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


def test_the_later_of_the_two_changes_is_the_one_a_run_has_to_clear():
    assert RA.INSTRUMENT_EPOCH > RA.SAMPLER_EPOCH


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
