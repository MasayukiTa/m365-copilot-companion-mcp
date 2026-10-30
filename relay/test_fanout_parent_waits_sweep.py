"""層B: sweep 側の状態遷移をミラーで試験する。

test_fanout_parent_waits.py が fanout.py の純粋関数 (層A) を試験するのに対し、
こちらは relay_fleet 側の振る舞い -- 分割元ワーカーが子の完了後に
『自分自身で』統合担当に復帰する経路 (adopt_parent_merge) を試験する。

オーナーの指摘 (2026-10-30 14:40):
  「fanout のときに、分割元が、分割したら『完了』で止まっているのが問題。
   完了を待たなければならない。統合を受け取る用の担当を立てるのは、違うと思う。」

ここで確かめるのは、分割元が待機したあと、受け取り専用の別ワーカーを
新たに作らず、分割元ワーカー自身 (同じオブジェクト) を統合担当に復帰させること。
"""
import pytest

from relay import fanout as fo
from relay import relay_fleet as rf


SPLIT_REPLY = """期間で分割します。

1. 2026年1月分の受信業務メールを取得する
2. 2026年2月分の受信業務メールを取得する
3. 2026年3月分の受信業務メールを取得する

%s""" % fo.SUBTASKS_READY


def _worker(goal="1〜3月のメールを一覧化する", fanout=True, spawn=None, depth=0):
    g = {"text": goal, "depth": depth}
    return rf.RelayWorker(g, "w0", fanout=fanout, spawn_fn=spawn)


def _records(n, *, outcomes=None):
    outs = outcomes or ["DONE"] * n
    return [{"finished": True, "outcome": outs[i], "subtask_index": i + 1,
             "result": "slice %d" % (i + 1)} for i in range(n)]


# ---- 1. 分割元が待機を経て自分自身で統合に復帰する --------------------------------

def test_a_waiting_parent_resumes_as_its_own_merge_worker():
    """adopt_parent_merge は、待機中 (awaiting_children) の分割元ワーカーそのものを
    統合担当に変える -- 別ワーカーを立てない。同じオブジェクトの status が
    ready に戻り、待機中に閉じた driver が再接続できるよう closed=False に戻る。"""
    w = _worker()
    got = []
    w._spawn_fn = lambda goal, kids, parent_checks=None, parent_partial="", **kw: got.append(kids)
    w._decide(SPLIT_REPLY)
    assert w.status == fo.AWAITING_CHILDREN          # まず待機に入る
    assert w.closed is True

    recs = _records(3)
    agg = fo.parent_resume_goal(w.goal, recs, campaign_id=w._fanin_campaign_id,
                                resume_conv="sess:PARENT", cwd="C:/x")
    w.adopt_parent_merge(agg, "sess:PARENT")

    assert w.status == "ready"                        # 非終端に戻り、次の sweep で走る
    assert w.closed is False                          # 再接続のため driver 状態を戻す
    assert w.resume_conv == "sess:PARENT"             # 分割元自身の会話を継続
    assert w.fanout is False                          # 統合ターンが再び分割を打診しない
    assert w._fanin_resumed is True                   # 二重復帰を防ぐ


def test_the_resumed_worker_role_is_parent_merge_not_aggregator():
    """復帰したワーカーの role は parent_merge (分割元自身の継続) であって、
    受け取り専用の別担当 (aggregator) ではない。"""
    w = _worker()
    recs = _records(2)
    agg = fo.parent_resume_goal(w.goal, recs, campaign_id=fo.campaign_id_for(w.goal),
                                resume_conv="sess:P")
    w.adopt_parent_merge(agg, "sess:P")
    env = w.task_envelope
    assert getattr(env, "role", "") == "parent_merge"
    assert getattr(env, "role", "") != "aggregator"


def test_the_resumed_goal_carries_the_parent_goal_and_names_failed_slices():
    """継続した統合 goal は親の目標を含み、失敗した子を名指しする
    (別担当でも分割元継続でも統合プロンプトの中身は変わらない)。"""
    goal = "社内一斉配信は除外し、日付/差出人/件名の形式で出力する"
    w = _worker(goal=goal)
    recs = _records(3, outcomes=["DONE", "STUCK", "DONE"])
    agg = fo.parent_resume_goal(goal, recs, campaign_id=fo.campaign_id_for(goal),
                                resume_conv="sess:P")
    w.adopt_parent_merge(agg, "sess:P")
    assert "社内一斉配信は除外" in w.goal         # 親の仕様を統合プロンプトに含む
    assert "2" in w.goal                              # 欠けたスライス番号が本文にある


# ---- 2. 会話 id が無ければ別担当にフォールバックする ------------------------------------

def test_without_a_parent_conversation_the_resume_goal_has_no_resume_conv():
    """分割元の会話 id が無い (別プロセスで家族を再構築した等) ときは、
    parent_resume_goal は resume_conv を落とす -- sweep 側はこれを見て別担当に
    フォールバックする (_resume_here が False になる)。"""
    goal = "1〜3月のメールを一覧化する"
    agg = fo.parent_resume_goal(goal, _records(2), campaign_id=fo.campaign_id_for(goal),
                                resume_conv="")
    assert not agg.get("resume_conv")


def test_a_present_parent_conversation_keeps_the_merge_with_the_splitter():
    """会話 id があれば resume_conv に載る -- sweep は分割元自身に統合させる。"""
    goal = "1〜3月のメールを一覧化する"
    agg = fo.parent_resume_goal(goal, _records(2), campaign_id=fo.campaign_id_for(goal),
                                resume_conv="sess:PARENT")
    assert agg["resume_conv"] == "sess:PARENT"


# ---- 3. 別担当継続と分割元継続は task_id が異なり、取り違えない ----------------------

def test_parent_merge_and_aggregator_have_distinct_task_ids():
    """分割元継続 (parent_merge, ...-parentmerge) と別担当 (aggregator, ...-merge) は
    task_id が異なる -- resume_key が重なって『統合済み」判定がズレ、二重統合
    するのを防ぐ。"""
    goal = "1〜3月のメールを一覧化する"
    cid = fo.campaign_id_for(goal)
    recs = _records(2)
    cont = fo.parent_resume_goal(goal, recs, campaign_id=cid, resume_conv="sess:P")
    agg = fo.aggregation_goal(goal, recs, campaign_id=cid)
    assert cont["task_id"] != agg["task_id"]
    assert cont["task_id"].endswith("-parentmerge")
    assert agg["task_id"].endswith("-merge")


# ---- 4. 待機中の分割元は active 計上から外れる (admission slot を湋らない) --------------

def test_awaiting_children_is_excluded_from_active_worker_count():
    """待機中の分割元はタブを解放済みなので、active_worker_count が数えては
    ならない -- aggregation_goal docstring が警告する『親が slot を握ったまま
    子を待つ」デッドロックを避けるため。"""
    w = _worker()
    w.status = fo.AWAITING_CHILDREN
    assert rf.active_worker_count([w]) == 0


def test_awaiting_children_is_not_a_terminal_status():
    """待機状態は終端 (done/stuck 等) ではない -- 通常実行に戻れる。"""
    assert fo.AWAITING_CHILDREN not in rf.TERMINAL
