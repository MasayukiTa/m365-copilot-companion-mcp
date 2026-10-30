"""分割元は、分割したら「完了」で止まらず、子が全部終わるまで待機し、
統合は分割元自身の会話で行う -- 統合専用の別担当は立てない。

オーナーの指摘 (2026-10-30 14:40):
  「fanout のときに、分割元が、分割したら『完了』で止まっているのが問題。
   完了を待たなければならない。統合を受け取る用の担当を立てるのは、違うと思う。」

このファイルは fanout.py の純粋関数を直接試験する (層A)。relay_fleet の sweep 側の
状態遷移 (層B) は test_fanout_parent_waits_sweep.py がミラーで試験する。
"""
import pytest

from relay import fanout as fo


READY = fo.SUBTASKS_READY


def _records(n, *, outcomes=None):
    """n 個の子の records。outcomes を渡すと各子の outcome を差し替える (既定は全 DONE)。"""
    outs = outcomes or ["DONE"] * n
    return [{"finished": True, "outcome": outs[i], "subtask_index": i + 1,
             "result": "slice %d" % (i + 1)} for i in range(n)]


# ---- 1. 分割元は分割後「完了」にならず待機する -------------------------------------------

def test_a_split_parent_is_not_finished():
    """分割が成功した分割元の fan-in 状態は『待機 (子の完了待ち)』であって『完了』ではない。
    outcome=FANOUT を terminal として扱う実装は、ここで False を返させてはならない。"""
    assert fo.parent_waits_for_children(spawned=True) is True


def test_a_parent_that_did_not_split_does_not_wait():
    """分割に至らなかった (子が queue されなかった) 分割元は待機しない -- 通常実行に戻る。"""
    assert fo.parent_waits_for_children(spawned=False) is False


def test_the_waiting_state_is_not_terminal():
    """待機状態を表す status 名は、通常実行に戻れる非終端の状態である。"""
    assert fo.AWAITING_CHILDREN not in ("done", "stuck", "maxturns", "error",
                                        "cancelled", "content_refused")


# ---- 2. 統合は分割元自身の会話で行う (別担当を立てない) -----------------------------------

def test_the_parent_itself_merges_in_its_own_conversation():
    """子が全部終わったら、分割元自身の会話 (resume_conv) の続きとして統合 goal を作る。
    role は親の継続であって、受け取り専用の aggregator ではない。"""
    recs = _records(3)
    goal = fo.parent_resume_goal("親の目標", recs, campaign_id="c1",
                                 resume_conv="sess:PARENT", cwd="C:/x")
    assert goal["resume_conv"] == "sess:PARENT"          # 分割元の会話を継続
    assert goal["role"] == "parent_merge"                # 受け取り専用担当ではない
    assert goal["campaign_id"] == "c1"
    assert "親の目標" in goal["text"]                     # 親の目標を統合プロンプトに含む
    assert goal.get("cwd") == "C:/x"


def test_the_resume_goal_is_not_a_separate_aggregator():
    """分割元自身の継続 goal の role は aggregator ではない (別担当の role と区別できる)。"""
    goal = fo.parent_resume_goal("親の目標", _records(2), campaign_id="c2",
                                 resume_conv="sess:P")
    assert goal["role"] != "aggregator"


def test_without_a_parent_conversation_it_cannot_resume_in_place():
    """分割元の会話 id が無い (別プロセスで家族を再構築した等) ときは、その場での継続が
    できないので resume_conv 無しの goal になる -- 呼び出し側はフォールバックで別担当を立てる。"""
    goal = fo.parent_resume_goal("親の目標", _records(2), campaign_id="c3",
                                 resume_conv="")
    assert not goal.get("resume_conv")


# ---- 3. 子が失敗・停止したら、分割元はその事実を受け取り、待ち続けない ---------------------

def test_a_failed_child_still_lets_the_parent_proceed():
    """子の一つが失敗 (非 DONE) でも『全員 finished』なら統合へ進める -- 待ち続けない。"""
    recs = _records(3, outcomes=["DONE", "STUCK", "DONE"])
    assert fo.ready_to_aggregate(recs) is True


def test_the_parent_resume_goal_names_the_failed_slice():
    """失敗した子は統合プロンプトで『未取得/未完了』として名指しされ、握りつぶされない。"""
    recs = _records(3, outcomes=["DONE", "STUCK", "DONE"])
    goal = fo.parent_resume_goal("親の目標", recs, campaign_id="c4",
                                 resume_conv="sess:P")
    assert "2" in goal["text"]            # 欠けたスライス番号が本文にある


def test_a_still_running_child_is_not_ready():
    """子がまだ動いている (finished=False) 間は、分割元は待機のまま統合に進まない。"""
    recs = _records(2)
    recs[1]["finished"] = False
    assert fo.ready_to_aggregate(recs) is False


# ---- 深さの制限なしで入れ子にできる (#361 の既存決定を保つ) -------------------------------

def test_a_nested_parent_also_waits():
    """入れ子の分割元 (depth>0) も同様に待機する -- 待機は深さに依らない。"""
    assert fo.parent_waits_for_children(spawned=True, depth=5) is True
