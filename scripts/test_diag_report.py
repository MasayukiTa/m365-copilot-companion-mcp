"""診断の読み取りテスト。区間の切り出しと、絶対値であることを固定する。"""
import json

from scripts.diag_report import analyse, window


def _write(tmp_path, name, events, samples):
    base = tmp_path / name
    (base.parent / (name + "_events.json")).write_text(
        json.dumps(events), encoding="utf-8")
    lines = ["ts,root_pid,n_procs,total_mb"]
    for ts, mb in samples:
        lines.append("%s,1,3,%s" % (ts, "" if mb is None else mb))
    (base.parent / (name + "_ws.csv")).write_text("\n".join(lines), encoding="utf-8")
    return str(base.parent / (name + "_events.json"))


def test_the_two_arms_are_split_by_their_own_wall_times(tmp_path):
    """1本目は冷えたブラウザ、2本目は温まったブラウザに当たる。境界を真ん中で切ると、
    長い方のアームの山が短い方に混ざり、比較したい当の量が壊れる。"""
    ev = {"transport": "tabs", "arm_order": "control,candidate",
          "control": {"wall_s": 100.0}, "candidate": {"wall_s": 300.0},
          "events": [{"event": "browser_rebuilt", "ts": 0.0},
                     {"event": "settled_fresh", "ts": 10.0},
                     {"event": "run_start", "ts": 10.0},
                     {"event": "run_end", "ts": 410.0}]}
    # 1本目(10-110)に高い山、2本目(110-410)は低い
    samples = [(0.0, 700.0), (5.0, 700.0), (10.0, 700.0),
               (50.0, 1500.0), (100.0, 1400.0),
               (150.0, 900.0), (300.0, 880.0), (405.0, 870.0)]
    out = analyse(_write(tmp_path, "d", ev, samples))
    assert out["cold_peak_mb"] == 1500.0, "1本目の山を取れていない"
    assert out["warm_peak_mb"] == 900.0, "2本目に1本目の山が混ざっている"
    assert out["cold_cost_mb"] == 800.0                      # 1500 - 700


def test_the_idle_period_is_measured_end_to_end(tmp_path):
    """受動的な減衰こそが、片方のレビュアーの主張の核。始点と終点を取り違えると、
    アームの残りかけを減衰として数えてしまう。"""
    ev = {"transport": "tabs", "arm_order": "control,candidate",
          "control": {"wall_s": 10.0}, "candidate": {"wall_s": 10.0},
          "events": [{"event": "browser_rebuilt", "ts": 0.0},
                     {"event": "settled_fresh", "ts": 5.0},
                     {"event": "run_start", "ts": 5.0},
                     {"event": "run_end", "ts": 25.0},
                     {"event": "idle_start", "ts": 25.0},
                     {"event": "idle_end", "ts": 85.0}]}
    samples = [(0.0, 700.0), (5.0, 700.0), (10.0, 1200.0), (24.0, 1100.0),
               (26.0, 1000.0), (55.0, 850.0), (84.0, 800.0)]
    out = analyse(_write(tmp_path, "e", ev, samples))
    assert out["idle_start_mb"] == 1000.0 and out["idle_end_mb"] == 800.0
    assert out["decay_mb"] == 200.0


def test_a_gap_in_the_trace_is_skipped_not_read_as_zero(tmp_path):
    """再構築中は CDP の持ち主が居ない。その空行を 0 と読むと、
    『ブラウザが一瞬で全部解放した』という嘘の谷ができる。"""
    ev = {"transport": "socket", "arm_order": "control,candidate",
          "control": {"wall_s": 10.0}, "candidate": {"wall_s": 10.0},
          "events": [{"event": "browser_rebuilt", "ts": 0.0},
                     {"event": "settled_fresh", "ts": 5.0},
                     {"event": "run_start", "ts": 5.0},
                     {"event": "run_end", "ts": 25.0}]}
    samples = [(0.0, None), (1.0, 700.0), (10.0, 900.0), (20.0, None), (24.0, 880.0)]
    out = analyse(_write(tmp_path, "f", ev, samples))
    assert out["samples"] == 3
    assert out["cold_peak_mb"] == 900.0


def test_window_is_inclusive_of_its_bounds():
    assert window([(1.0, 10.0), (2.0, 20.0), (3.0, 30.0)], 1.0, 2.0) == [10.0, 20.0]
