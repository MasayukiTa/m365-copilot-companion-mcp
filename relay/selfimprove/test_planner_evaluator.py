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

def test_the_threshold_clears_the_largest_gap_two_identical_arms_produced():
    """0.25 は4ゴールが表現できる最小差だが、同一プログラムの腕どうしが
    日常的に到達する -- 231ペア中26%が非ゼロで、最大 0.500。
    閾値はそれに乗るのではなく越えなければならない。"""
    assert PE.MIN_TURNS_GAIN > PE.NULL_SPREAD_OBSERVED["max_pair_difference"]
    assert PE.MIN_TURNS_GAIN == 0.75


def test_the_derivation_is_recorded_rather_than_asserted():
    """『測った』と書くだけなら、後の読み手は確かめようがない。"""
    obs = PE.NULL_SPREAD_OBSERVED
    assert obs["arms"] == 22 and obs["goals"] == 89
    src = inspect.getsource(PE)
    i = src.index("MIN_TURNS_GAIN = 0.75")
    block = src[max(0, i - 2200):i]
    assert "0.000" in block and "resolution rather than its noise" in block


def test_the_two_dedicated_nulls_were_not_taken_at_face_value():
    """専用の帰無走行2本はどちらも 0.000 を返した。
    その8ゴールが全部1ターンで終わったからで、
    計器に振れ幅が無かっただけ -- 額面で受け取れば、
    ノイズより細かい目盛りで読む閾値になっていた。"""
    src = inspect.getsource(PE)
    i = src.index("MIN_TURNS_GAIN = 0.75")
    assert "no room to vary" in src[max(0, i - 2200):i]


def test_a_calibrated_instrument_no_longer_refuses():
    assert PE.preflight(free_mb=8000.0, observable_recorded=True) == []


def test_the_floor_is_named_as_a_property_of_these_goals():
    """4ゴール・ほぼ1ターンという作業負荷の性質。
    3-4ターンかかるゴール集合では別の広がりになる。"""
    src = inspect.getsource(PE)
    i = src.index("MIN_TURNS_GAIN = 0.75")
    assert "REVISIT IF THE GOALS CHANGE" in src[max(0, i - 2200):i]


def test_a_calibrated_instrument_decides():
    got = PE.decide(_arm(turns=12), _arm(turns=6), min_gain=1.0)
    assert got["verdict"] == "keep" and got["turns_gain"] == 1.5


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


# ---- 計器の選択と、数えた分で割ること -------------------------------------------------------------

def test_the_runner_picks_the_instrument_that_can_judge_the_difference():
    """`run` は常にメモリの判定器を呼んでいた。planner の比較は turns を動かし
    Edge メモリを動かす理由が無いので、届かない300MB閾値で採点され、
    誰も測っていない量についての数字と共に INCONCLUSIVE を返すところだった。"""
    from relay.selfimprove import route_evaluator as RV
    base = M.base_manifest()
    p2 = M.apply_genome(base, {"components": {"planner": "planner/v2"}})
    t2 = M.apply_genome(base, {"components": {"transport": "transport/v2"}})
    assert C.instrument_for_pair(p2, base) is PE
    assert C.instrument_for_pair(t2, base) is RV


def test_a_difference_spanning_two_instruments_picks_neither():
    """どの定規で測るかを黙って決めない。"""
    base = M.base_manifest()
    both = M.apply_genome(base, {"components": {"planner": "planner/v2",
                                                "transport": "transport/v2"}})
    assert C.instrument_for_pair(both, base) is None


def test_the_verdict_reads_the_quantity_its_instrument_measures():
    """turns の比較で memory_gain_mb を読めば毎回ゼロで、
    それを『差なし』として報告することになる。"""
    a = {"turns_gain": 1.2, "control": {"done": 4, "goals": 4},
         "candidate": {"done": 4, "goals": 4}}
    got = C.decide(a, dict(a, turns_gain=1.1), instrument=PE)
    assert got["verdict"] == C.VERDICT_A, got


def test_an_uncalibrated_instrument_stops_the_comparison(monkeypatch):
    monkeypatch.setattr(PE, "MIN_TURNS_GAIN", None)
    a = {"turns_gain": 5.0, "control": {"done": 4, "goals": 4},
         "candidate": {"done": 4, "goals": 4}}
    got = C.decide(a, a, instrument=PE)
    assert got["verdict"] == C.VERDICT_NONE and got.get("aborted") is True
    assert "no measured floor" in got["why"]


def test_turns_are_divided_by_what_was_counted_not_by_what_was_sent():
    """4ターンを4ゴールで割れば 1.0、同じ4ターンをログに残った3ゴールで割れば 1.33。
    ハーネスの性質についての主張なのは片方だけ。"""
    assert PE.turns_per_goal({"turns": 4, "goals": 4, "logged_goals": 4}) == 1.0
    assert round(PE.turns_per_goal({"turns": 4, "goals": 4, "logged_goals": 3}), 2) == 1.33
    # logged_goals が無い呼び出し元(テストや古い行)は goals に落ちる
    assert PE.turns_per_goal({"turns": 4, "goals": 4}) == 1.0
    assert PE.turns_per_goal({"turns": 0, "goals": 0}) is None


def test_the_arm_carries_the_other_instruments_quantity_too():
    """腕を走らせるのは高価で計器に依存しない。読む数字だけが違う。"""
    import inspect
    from relay.selfimprove import scheduler as S
    src = inspect.getsource(S.route_evaluator_for)
    assert '"turns_gain": _turns_gain(control, candidate)' in src


def test_the_nightly_path_also_picks_the_instrument():
    """`compare.run` を直して、この adapter -- nightly が通る方 -- は
    まだ全部 RV.decide を呼んでいた。同じ欠陥が1経路隣にあった。"""
    import inspect
    from relay.selfimprove import scheduler as S
    src = inspect.getsource(S.route_evaluator_for)
    assert "judge = _judge_for(candidate_manifest" in src
    assert "judge.decide(control, candidate)" in src
    assert "RV.decide(control, candidate)" not in src


def test_the_result_says_which_ruler_produced_it():
    """判定を受け取った側が、どの定規で測ったか言えなければ帰属できない。"""
    import inspect
    from relay.selfimprove import scheduler as S
    assert '"instrument": judge.__name__' in inspect.getsource(S.route_evaluator_for)


def test_a_planner_candidate_selects_the_turns_judge():
    from relay.selfimprove import compare as C
    base = M.base_manifest()
    cand = M.apply_genome(base, {"components": {"planner": "planner/v2"}})
    assert C.instrument_for_pair(cand, base) is PE


# ---- 大きく負の差は「分からなかった」ではない -----------------------------------------------------

def test_a_candidate_that_costs_a_full_turn_is_rejected_not_shrugged_at():
    """最初の版は帰無の場合と混ぜ、-1.00 を『閾値0.75未満』と書いた -- 逆向きに1.33倍。
    候補が1ゴールあたり丸1ターン余計に使うと検出することが、
    この計器を作った理由そのもの。それを『分からなかった』として捨てていた。"""
    a = lambda t: {"turns": t, "goals": 4, "logged_goals": 4, "done": 4}
    got = PE.decide(a(4), a(8))
    assert got["verdict"] == "reject"
    assert "ROSE by 1.00" in got["why"]
    assert "measured cost, not an absence of evidence" in got["why"]


def test_the_null_region_says_inside_rather_than_under():
    """『under the floor』は符号のある数に対して嘘になる。"""
    a = lambda t: {"turns": t, "goals": 4, "logged_goals": 4, "done": 4}
    got = PE.decide(a(4), a(5))
    assert got["verdict"] == "inconclusive"
    assert "inside the" in got["why"] and "under the" not in got["why"]


def test_the_memory_judge_has_the_same_three_regions():
    """同じ文言が route_evaluator にもあり、今日の -666 MB の走行を
    inconclusive と誤報していた。読んだ私もそのまま通した。"""
    from relay.selfimprove import route_evaluator as RV
    a = lambda p: {"done": 4, "goals": 4, "peak_mb": p}
    assert RV.decide(a(600), a(200))["verdict"] == "keep"
    assert RV.decide(a(200), a(866))["verdict"] == "reject"
    assert RV.decide(a(400), a(500))["verdict"] == "inconclusive"


# ---- 記録されなかった腕を数字に変えないこと --------------------------------------------------------

def test_an_arm_that_logged_nothing_is_not_zero_turns():
    """`or goals` のフォールバックが『0ターン÷4ゴール=0.0』という
    存在しない測定値を作り、そこから -1.00 が出た --
    『数えた分で割る』という注意を書いた、その変更自身が。"""
    assert PE.turns_per_goal({"turns": 0, "goals": 4, "logged_goals": 0}) is None
    got = PE.decide({"turns": 0, "goals": 4, "logged_goals": 0, "done": 4},
                    {"turns": 4, "goals": 4, "logged_goals": 4, "done": 4})
    assert got.get("aborted") is True
    assert got["verdict"] == "inconclusive"


def test_a_row_without_logged_goals_still_divides_by_goals():
    """logged_goals を持たない古い行やテストは、これまで通り goals で割る。"""
    assert PE.turns_per_goal({"turns": 4, "goals": 4}) == 1.0


def test_the_nightly_path_enables_the_route_on_both_arms():
    """対照腕がタブだと worker_done が書かれず、turns 計器は何も数えられない。
    最初の planner 走行がまさにそれで、捏造された 0.0 を判定に渡した。"""
    import io as _io
    src = _io.open("scripts/run_nightly_real.py", encoding="utf-8").read()
    assert "control_socket=True" in src
