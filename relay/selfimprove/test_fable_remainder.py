"""Fable の助言のうち未実装だった2件。どちらも「まだ観測されていない」を明示するのが要点。

1. クラス別集計 -- ただし Simpson のパラドックスではない。両腕が同じゴール集合を走るので
   群サイズの偏りは構造的に起きない。起きるのは打ち消しで、症状は同じくらい悪い。
2. fallback の冪等性 -- 再送が安全かは理由による。判断は変えず、曖昧さを数えられるようにする。
"""
import json

import pytest

from relay import transport_policy as TP
from relay.selfimprove import planner_evaluator as PE


# ---- クラス別: 打ち消しを1つの数字に潰さない -------------------------------------------------------

def _goals():
    from scripts.run_route_campaign import GOALS
    return GOALS


def test_the_classes_come_from_a_structural_property_not_a_new_predicate():
    """`needs_workiq` は並行セッションが削除済み。再発明せず、
    ゴールが受入検証を持つかどうかで分ける -- これはゴール自身の性質。"""
    from relay.relay_fleet import goal_fields
    seen = {PE.class_of(goal_fields(g)[0], _goals()) for g in _goals()}
    assert seen == {PE.VERIFIED, PE.UNVERIFIED}
    assert not hasattr(TP, "needs_workiq"), "消えた述語に戻っている"


def test_an_unrecognised_goal_is_unverified_rather_than_an_error():
    assert PE.class_of("何か別のゴール", _goals()) == PE.UNVERIFIED
    assert PE.class_of(None, _goals()) == PE.UNVERIFIED


def test_opposite_movements_that_cancel_are_flagged():
    """片方のクラスを助け、もう片方を同じだけ害する候補は平均するとゼロになり、
    走行は『差なし』と報告する -- 2つのことを逆向きに変えたハーネスについて。"""
    control = {PE.VERIFIED: {"goals": 2, "turns": 4}, PE.UNVERIFIED: {"goals": 2, "turns": 2}}
    candidate = {PE.VERIFIED: {"goals": 2, "turns": 2}, PE.UNVERIFIED: {"goals": 2, "turns": 4}}
    got = PE.classes_disagree(control, candidate)
    assert got["disagree"] is True
    assert got["per_class"][PE.VERIFIED] == 1.0
    assert got["per_class"][PE.UNVERIFIED] == -1.0
    assert "cancellation" in got["why"]


def test_classes_moving_the_same_way_are_not_flagged():
    """過剰に鳴らすと、本当に打ち消しているときに読まれなくなる。"""
    control = {PE.VERIFIED: {"goals": 2, "turns": 4}, PE.UNVERIFIED: {"goals": 2, "turns": 4}}
    candidate = {PE.VERIFIED: {"goals": 2, "turns": 2}, PE.UNVERIFIED: {"goals": 2, "turns": 2}}
    assert PE.classes_disagree(control, candidate)["disagree"] is False


def test_movements_inside_the_floor_do_not_count_as_disagreement():
    """ノイズの内側で符号が割れるのは、打ち消しではなくノイズ。"""
    control = {PE.VERIFIED: {"goals": 4, "turns": 5}, PE.UNVERIFIED: {"goals": 4, "turns": 4}}
    candidate = {PE.VERIFIED: {"goals": 4, "turns": 4}, PE.UNVERIFIED: {"goals": 4, "turns": 5}}
    assert PE.classes_disagree(control, candidate)["disagree"] is False


def test_a_class_with_nothing_logged_is_not_invented():
    control = {PE.VERIFIED: {"goals": 0, "turns": 0}, PE.UNVERIFIED: {"goals": 2, "turns": 2}}
    candidate = {PE.VERIFIED: {"goals": 2, "turns": 2}, PE.UNVERIFIED: {"goals": 2, "turns": 2}}
    got = PE.classes_disagree(control, candidate)
    assert got["per_class"][PE.VERIFIED] is None


def test_the_label_simpsons_paradox_is_not_borrowed():
    """群サイズが等しいので古典的な反転は起きない。
    起きるのは打ち消しで、正しい名前で呼ぶ。"""
    import inspect
    src = inspect.getsource(PE)
    i = src.index("NOT SIMPSON'S PARADOX")
    block = src[i:i + 700]
    assert "both arms here run the SAME goals" in block
    assert "cannot occur" in block


def test_turns_by_class_splits_the_log(tmp_path):
    from relay.relay_fleet import goal_fields
    goals = _goals()
    verified = goal_fields(goals[2])[0]
    unverified = goal_fields(goals[0])[0]
    p = tmp_path / "socket_route.jsonl"
    rows = [
        {"event": "worker_done", "ts": 10.0, "turns": 3, "outcome": "DONE", "goal": verified},
        {"event": "worker_done", "ts": 11.0, "turns": 1, "outcome": "DONE", "goal": unverified},
        {"event": "worker_done", "ts": 12.0, "turns": 2, "outcome": "STUCK", "goal": unverified},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    got = PE.turns_by_class(str(p), goals)
    assert got[PE.VERIFIED] == {"goals": 1, "turns": 3, "done": 1}
    assert got[PE.UNVERIFIED] == {"goals": 2, "turns": 3, "done": 1}


# ---- fallback の冪等性: 曖昧さを数えられるようにする ------------------------------------------------

def test_a_turn_that_had_already_landed_is_named_as_such():
    """『the turn completed but carried no text』はモデルが動いた後の話。
    再送はその行為をもう一度頼むことになる。"""
    assert TP.delivery_status("the turn completed but carried no text") == "delivered"
    assert TP.delivery_status("consent card appeared") == "delivered"
    assert TP.duplicate_risk("the turn completed but carried no text") is True


def test_a_failure_before_the_send_is_safe_to_repeat():
    for reason in ("token refresh failed", "401 unauthorized", "capture failed",
                   "handshake timeout"):
        assert TP.delivery_status(reason) == "not_delivered", reason
        assert TP.duplicate_risk(reason) is False, reason


def test_a_dropped_connection_stays_unknown_and_counts_as_a_risk():
    """途中で切れたフレームは届いたかもしれない。
    『届いていない』に寄せれば再送を黙って安全と認定し、
    『届いた』に寄せれば重複していない事象で件数を膨らませる。"""
    assert TP.delivery_status("ConnectionClosed: 1006") == "unknown"
    assert TP.delivery_status("") == "unknown"
    assert TP.duplicate_risk("ConnectionClosed: 1006") is True


def test_the_record_carries_the_delivery_status():
    """理由から導けたのに書かれておらず、この問いは毎回
    後から散文を読み直して答えるしかなかった。"""
    import inspect
    from relay import relay_fleet as RF
    src = inspect.getsource(RF.RelayWorker._fall_back_to_tab)
    assert "delivery=delivery" in src
    assert "duplicate_risk=" in src
    assert "cause=cause" in src


def test_the_overstated_claim_was_corrected():
    """『THE GOAL IS NOT AFFECTED』は、送信が届いていた場合には偽。"""
    import inspect
    from relay import relay_fleet as RF
    doc = inspect.getdoc(RF.RelayWorker._fall_back_to_tab) or ""
    assert "THE GOAL IS NOT AFFECTED" not in doc
    assert "depends on the reason" in doc.lower()


def test_the_resend_behaviour_is_deliberately_unchanged():
    """観測されたフォールバックはゼロ。確実な『1ターン喪失』を、
    誰も測っていない危険と引き換えにするのは改善ではない。"""
    import inspect
    from relay import relay_fleet as RF
    doc = inspect.getdoc(RF.RelayWorker._fall_back_to_tab) or ""
    assert "re-send is unchanged" in doc
    assert "no fallback has fired" in doc


# ---- 判定に届いていること（計算できるだけでは分岐が通らない） ---------------------------------------

def test_the_cancellation_check_reaches_the_verdict():
    """計算できるだけの検査は、絶対に通らない分岐 --
    このリポジトリが6コンポーネントで見つけた欠陥。"""
    a = {"turns": 6, "goals": 4, "logged_goals": 4, "done": 4}
    pc = {"control": {PE.VERIFIED: {"goals": 2, "turns": 4},
                      PE.UNVERIFIED: {"goals": 2, "turns": 2}},
          "candidate": {PE.VERIFIED: {"goals": 2, "turns": 2},
                        PE.UNVERIFIED: {"goals": 2, "turns": 4}}}
    got = PE.decide(a, a, per_class=pc)
    assert got["aborted"] is True
    assert "two findings that cancelled" in got["why"]
    assert got["per_class"] == {PE.VERIFIED: 1.0, PE.UNVERIFIED: -1.0}


def test_a_comparison_without_a_breakdown_still_gets_a_verdict():
    """内訳を割れない比較でも判定は出る。根拠が少ないだけ。"""
    a = {"turns": 4, "goals": 4, "logged_goals": 4, "done": 4}
    got = PE.decide(a, a)
    assert got["verdict"] == "inconclusive" and got.get("per_class") is None


def test_the_arm_carries_the_breakdown():
    import inspect
    from relay.selfimprove import scheduler as S
    src = inspect.getsource(S.route_evaluator_for)
    assert '"by_class": by_class' in src
    assert "per_class=per_class" in src


def test_the_memory_judge_is_called_without_a_breakdown():
    """Edge メモリはゴール単位に帰属できないので割るものが無い。
    旧来の呼び方が正しく、フォールバックではない。"""
    import inspect
    from relay.selfimprove import scheduler as S
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("except TypeError:")
    assert "not a fallback" in src[i:i + 300]
