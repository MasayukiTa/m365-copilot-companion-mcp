"""2つ目の計器。1つ目を広げるのではなく、兄弟を作る。

route_evaluator は Edge の commit 増分を測る。それはフリートが開くレンダラー数に反応し、
他の何にも反応しない。planner の変更をどれだけ長く走らせても見えない --
20分かけて構造的な理由で INCONCLUSIVE になる。計器は壊れていないし広げたくもない。

使い回すのは手順のほうで、そこに今日の教訓が入っている:
帰無走行が先 / 閾値はその広がりから / 範囲を宣言 / 両順序 / 壊れた腕は INFRA。
"""
import inspect
import json

import pytest

from relay.selfimprove import compare as C
from relay.selfimprove import manifest as M
from relay.selfimprove import planner_evaluator as PE


def _arm(goals=4, turns=8, done=4, **kw):
    row = {"goals": goals, "turns": turns, "done": done}
    row.update(kw)
    return row


# ---- 較正されるまで判定しない ---------------------------------------------------------------------

def test_the_threshold_starts_unset():
    """メモリの床は、無関係な定数からの借り物として1日を過ごし、
    腕の順序を測っていたと判明した導出を生き延び、
    帰無走行が広がりを与えて初めて意味を持った。
    未較正で始めるのは後で直す手落ちではなく、
    帰無走行が済むまで真実を言っている状態。"""
    assert PE.MIN_TURNS_GAIN is None


def test_deciding_before_calibration_raises_rather_than_returning_inconclusive():
    """INCONCLUSIVE を返すと『測ったが何も無かった』と区別がつかない。
    この装置全体が引き離そうとしている2つの主張がそれ。"""
    with pytest.raises(PE.NotCalibrated):
        PE.decide(_arm(turns=12), _arm(turns=6))


def test_preflight_names_the_missing_calibration():
    reasons = PE.preflight(free_mb=8000.0)
    assert any("no measured noise floor" in r for r in reasons), reasons
    assert any("null pass" in r for r in reasons), reasons


def test_a_calibrated_instrument_decides():
    got = PE.decide(_arm(turns=12), _arm(turns=6), min_gain=1.0)
    assert got["verdict"] == "keep" and got["turns_gain"] == 1.5


def test_the_reason_the_threshold_is_unset_is_written_down():
    src = inspect.getsource(PE)
    i = src.index("MIN_TURNS_GAIN = None")
    block = src[max(0, i - 1400):i]
    assert "null run" in block and "invented" in block


# ---- 判定規則 -----------------------------------------------------------------------------------

def test_fewer_turns_by_finishing_less_is_not_an_improvement():
    got = PE.decide(_arm(turns=12, done=4), _arm(turns=4, done=2), min_gain=0.5)
    assert got["verdict"] == "reject"
    assert "finishing less" in got["why"]


def test_a_gain_inside_the_noise_is_inconclusive():
    got = PE.decide(_arm(turns=9), _arm(turns=8), min_gain=1.0)
    assert got["verdict"] == "inconclusive"
    assert "not a finding" in got["why"]


def test_an_arm_that_completed_nothing_has_no_average_to_compare():
    got = PE.decide(_arm(goals=0, turns=0, done=0), _arm(), min_gain=1.0)
    assert got.get("aborted") is True


# ---- 経路の規則は書き直さず、そのまま使う ---------------------------------------------------------

def test_the_route_rule_is_reused_not_restated():
    """腕が走行途中で自分でなくなる問題は、測る量とは無関係に同じ規則。
    2実装に分かれるのは harness_id の穴と同型。"""
    src = inspect.getsource(PE.decide)
    assert "RV.fallback_verdict" in src
    assert "closed_reason" not in src


def test_a_closed_route_stops_a_turns_verdict_too():
    got = PE.decide(_arm(turns=12),
                    _arm(turns=6, route_closed_reason="3 consecutive failures"),
                    min_gain=1.0)
    assert got["verdict"] == "inconclusive" and got["aborted"] is True


# ---- 観測量は既存ログから読む ---------------------------------------------------------------------

def test_turns_are_read_from_the_log_the_fleet_already_writes(tmp_path):
    """観測量が既に per-goal で durable に残っていることが、
    6座標のうちこれを最安にした理由。"""
    p = tmp_path / "socket_route.jsonl"
    rows = [
        {"event": "worker_done", "ts": 100.0, "turns": 2, "outcome": "DONE", "route": "socket"},
        {"event": "worker_done", "ts": 101.0, "turns": 3, "outcome": "DONE", "route": "socket"},
        {"event": "worker_done", "ts": 102.0, "turns": 5, "outcome": "STUCK", "route": "socket"},
        {"event": "fallback", "ts": 103.0, "reason": "x"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    got = PE.turns_from_log(str(p))
    assert got == {"goals": 3, "turns": 10, "done": 2}


def test_rows_before_the_arm_started_are_not_counted(tmp_path):
    """両腕が同じログに追記するので、時刻で切らないと前の腕の行を数える。"""
    p = tmp_path / "socket_route.jsonl"
    rows = [
        {"event": "worker_done", "ts": 100.0, "turns": 9, "outcome": "DONE"},
        {"event": "worker_done", "ts": 200.0, "turns": 2, "outcome": "DONE"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert PE.turns_from_log(str(p), since_ts=150.0) == {"goals": 1, "turns": 2, "done": 1}


def test_a_missing_log_is_zero_rather_than_an_exception(tmp_path):
    assert PE.turns_from_log(str(tmp_path / "nope.jsonl"))["goals"] == 0


# ---- 計器の登録と選択 ---------------------------------------------------------------------------

def test_the_comparison_picks_the_instrument_by_component():
    assert C.instrument_for("planner") is PE
    from relay.selfimprove import route_evaluator as RV
    assert C.instrument_for("transport") is RV
    assert C.instrument_for("memory") is None


def test_the_declared_range_now_covers_both():
    measures, note = C.instrument_measures()
    assert set(measures) == {"transport", "planner"}
    assert "turns" in note and "Edge" in note


def test_a_planner_difference_is_now_visible_to_the_comparison():
    base = M.base_manifest()
    v2 = M.apply_genome(base, {"components": {"planner": "planner/v2"}})
    visible, why = C.instrument_can_see(v2, base)
    assert visible is True and "planner" in why


# ---- 版が本当に違うかの検査が成分横断であること -----------------------------------------------------

def test_the_behavioural_check_covers_planner_not_only_transport():
    """transport 専用だったので、planner で違う枝は素通りしていた。
    同じ欠陥が1成分隣にあった。"""
    from scripts.run_route_campaign import GOALS
    base = M.base_manifest()
    v2 = M.apply_genome(base, {"components": {"planner": "planner/v2"}})
    differ, why = C.versions_differ("planner", v2, base, GOALS)
    assert differ is True
    assert "4 of 4" in why


def test_a_component_without_a_probe_says_so_rather_than_assuming():
    base = M.base_manifest()
    v2 = M.apply_genome(base, {"components": {"memory": "memory/v2"}})
    differ, why = C.versions_differ("memory", v2, base, ["x"])
    assert differ is True
    assert "no behavioural probe" in why


def test_one_real_difference_is_enough_even_if_another_component_is_inert():
    """差のある成分が1つでもあれば両腕は2つのプログラム。
    全成分が同一挙動のときだけ拒否する。"""
    src = inspect.getsource(C.refusals)
    assert "any_real" in src
    assert "not any_real" in src


def test_a_run_that_cannot_record_turns_is_refused_before_it_starts():
    """`worker_done` は経路が有効なときしか書かれない。両腕タブの走行は
    数えるべき行を1つも残さないまま、20分間まったく正常に見える。
    監査文書にその注意を書いた本人が、その直後にその走行を起動して30分待った。"""
    reasons = PE.preflight(free_mb=8000.0, calibrated=1.0, observable_recorded=False)
    assert any("worker_done" in r for r in reasons), reasons
    assert any("both arms on tabs" in r for r in reasons), reasons


def test_a_recordable_and_calibrated_run_is_allowed():
    assert PE.preflight(free_mb=8000.0, calibrated=1.0, observable_recorded=True) == []
