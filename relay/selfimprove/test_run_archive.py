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
    before = RA.INSTRUMENT_EPOCH - 100
    after = RA.INSTRUMENT_EPOCH + 100
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
    _write(archive, "route-transport-v1-NULL-ctrlfirst-%d" % (RA.WORKLOAD_EPOCH["multiturn"] + 5),
           null=True, gain=196.8, goals="multiturn")
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
