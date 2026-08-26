"""The rules a split has to obey before it becomes several conversations.

Fan-out turns one goal into many live conversations, so a mis-read reply is not a wasted turn
-- it is a dozen of them, doing work nobody asked for. These tests are written as the
questions that decide whether a proposed split is safe to act on at all.
"""
import pytest

from relay import fanout as fo


READY = fo.SUBTASKS_READY

SPLIT = """この目標は期間で分割できます。

1. 2026年1月分の受信業務メールを取得する
2. 2026年2月分の受信業務メールを取得する
3. 2026年3月分の受信業務メールを取得する

%s""" % READY


# ---- recognising a split ------------------------------------------------------------------

def test_the_marker_is_recognised():
    assert fo.fanout_ready(SPLIT) is True


def test_a_reply_still_being_written_is_not_a_split():
    assert fo.fanout_ready("分割案を考えています") is False
    assert fo.fanout_ready("") is False


def test_the_steps_come_out_in_order():
    assert fo.subtasks_from(SPLIT) == [
        "2026年1月分の受信業務メールを取得する",
        "2026年2月分の受信業務メールを取得する",
        "2026年3月分の受信業務メールを取得する",
    ]


# ---- refusing a split that cannot be acted on ---------------------------------------------

def test_one_item_is_not_a_split():
    """Accepting it would let a goal alternate between splitting and working, forever."""
    assert fo.subtasks_from("1. 全部やる\n%s" % READY) == []


def test_an_absurd_number_of_pieces_is_refused():
    """Sixty children is a mis-parse or an agent listing every record it means to fetch."""
    body = "\n".join("%d. 対象%d の業務メールを取得する" % (i, i) for i in range(1, 61))
    assert fo.subtasks_from(body + "\n" + READY) == []


def test_fragments_are_dropped_and_may_sink_the_split():
    """'2月' tells a conversation that has never seen this one nothing it can act on."""
    body = "1. 2月\n2. 3月\n3. 2026年1月分の受信業務メールを取得する\n" + READY
    assert fo.subtasks_from(body) == []


def test_a_repeated_step_is_not_run_twice():
    """Two conversations doing identical work would double-count every row on merge."""
    body = ("1. 2026年1月分の受信業務メールを取得する\n"
            "2. 2026年1月分の受信業務メールを取得する\n"
            "3. 2026年2月分の受信業務メールを取得する\n" + READY)
    assert len(fo.subtasks_from(body)) == 2


def test_prose_with_no_list_yields_nothing():
    assert fo.subtasks_from("分割は不要です。このまま進めます。%s" % READY) == []


# ---- what a child inherits ----------------------------------------------------------------

def test_a_child_carries_the_parents_instructions_not_just_its_slice():
    """A child's conversation has never seen the parent's. Handed only '2月分を取得する' it
    does not know the format, the exclusions, or where output belongs -- and invents them."""
    parent = "社内一斉配信は除外し、日付/差出人/件名/要旨の形式で出力すること"
    kids = fo.child_goals(parent, ["2026年1月分を取得する", "2026年2月分を取得する"])
    assert len(kids) == 2
    for k in kids:
        assert parent in k["text"]
    assert "2026年1月分を取得する" in kids[0]["text"]
    assert "2026年2月分を取得する" not in kids[0]["text"]


def test_a_child_is_told_to_stay_in_its_lane():
    kids = fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"])
    assert "手を出さないこと" in kids[0]["text"]
    assert "1/2" in kids[0]["text"] and "2/2" in kids[1]["text"]


def test_children_share_one_campaign_and_name_their_parent():
    kids = fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"],
                          parent_task_id="t-parent")
    assert len({k["campaign_id"] for k in kids}) == 1
    assert all(k["parent_task_id"] == "t-parent" for k in kids)
    assert [k["subtask_index"] for k in kids] == [1, 2]


def test_the_campaign_id_is_derived_from_the_goal_so_a_resume_rejoins_the_family():
    a = fo.child_goals("同じ目標", ["範囲A を取得する", "範囲B を取得する"])
    b = fo.child_goals("同じ目標", ["範囲A を取得する", "範囲B を取得する"])
    assert a[0]["campaign_id"] == b[0]["campaign_id"]
    c = fo.child_goals("別の目標", ["範囲A を取得する", "範囲B を取得する"])
    assert c[0]["campaign_id"] != a[0]["campaign_id"]


def test_children_do_not_split_again():
    """Recursive splitting is how one runaway goal becomes an unbounded number of chats."""
    assert fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"],
                          depth=fo.MAX_DEPTH) == []


def test_a_child_inherits_acceptance_checks_and_cwd():
    kids = fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"],
                          checks=[{"kind": "file"}], cwd="C:/x")
    assert kids[0]["checks"] == [{"kind": "file"}]
    assert kids[0]["cwd"] == "C:/x"


# ---- putting the answers back together ----------------------------------------------------

def _r(i, outcome, result):
    return {"subtask_index": i, "outcome": outcome, "result": result}


def test_the_parent_is_given_every_child_report():
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "1月は120件"), _r(2, "DONE", "2月は98件")])
    assert "元の目標" in p
    assert "1月は120件" in p and "2月は98件" in p
    assert "DONE" in p


def test_failures_are_named_rather_than_quietly_dropped():
    """A summary that reads as complete because its gaps were never mentioned is the exact
    defect the adversarial reviews kept finding in this work."""
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "1月は120件"), _r(2, "STUCK", "")])
    assert "未完了" in p
    assert "未取得" in p


def test_a_clean_sweep_says_so():
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert "全サブタスクが完了" in p
    assert "未完了のサブタスク" not in p


def test_a_huge_child_report_is_truncated_so_the_merge_turn_still_fits():
    """The merge must not itself exhaust the conversation it runs in -- which is the whole
    condition fan-out exists to avoid."""
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "x" * 50000), _r(2, "DONE", "b")],
                              limit_each=500)
    assert len(p) < 5000
    assert "以下略" in p


def test_the_merge_asks_for_a_report_not_a_pile_of_reports():
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert "そのまま並べる" in p
    assert p.rstrip().endswith("DONE と書いてください。") or "DONE と書いてください" in p


# ---- the instruction the agent is given ---------------------------------------------------

def test_the_split_request_forbids_relative_slices():
    """'残りを続ける' is unusable to a conversation that cannot see what came before."""
    assert "相対的な指示は不可" in fo.SPLIT_JOB
    assert READY in fo.SPLIT_JOB


def test_the_split_request_states_the_bounds_it_will_be_judged_by():
    assert str(fo.MIN_CHILDREN) in fo.SPLIT_JOB
    assert str(fo.MAX_CHILDREN) in fo.SPLIT_JOB


# ---- when a campaign is finished, and what merges it ---------------------------------------

def test_a_campaign_is_ready_only_when_every_child_has_finished():
    assert fo.ready_to_aggregate([{"finished": True}, {"finished": True}]) is True
    assert fo.ready_to_aggregate([{"finished": True}, {"finished": False}]) is False


def test_no_children_is_not_a_finished_campaign():
    """Merging nothing produces a confident summary of work that never ran."""
    assert fo.ready_to_aggregate([]) is False


def test_the_merge_is_its_own_goal_not_a_turn_on_the_parent():
    """A parent parked waiting for its children holds an admission slot while it waits; with
    a concurrency cap below the number of children that is a deadlock."""
    g = fo.aggregation_goal("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert g["role"] == "aggregator"
    assert "元の目標" in g["text"]
    assert g["priority"] is True


def test_the_merge_never_splits_again():
    g = fo.aggregation_goal("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert g["depth"] >= fo.MAX_DEPTH


def test_the_merge_joins_the_campaign_it_merges():
    kids = fo.child_goals("元の目標", ["範囲A を取得する", "範囲B を取得する"])
    g = fo.aggregation_goal("元の目標", [_r(1, "DONE", "a")])
    assert g["campaign_id"] == kids[0]["campaign_id"]
    assert g["task_id"].endswith("-merge")


# ---- a slice that was retried ---------------------------------------------------------------

def test_a_retried_slice_reports_the_attempt_that_worked():
    """The failed attempt and its retry are two records for one range. Reporting both would
    tell the merge that range failed -- and the merge is required to name failures, so it
    would mark 未取得 a range sitting completed in the next record."""
    recs = [_r(1, "DONE", "1月は120件"),
            _r(2, "STUCK", ""),
            _r(2, "DONE", "2月は98件")]
    out = fo.collapse_retries(recs)
    assert [r["subtask_index"] for r in out] == [1, 2]
    assert out[1]["outcome"] == "DONE"
    assert out[1]["result"] == "2月は98件"


def test_a_slice_that_never_succeeded_stays_failed():
    out = fo.collapse_retries([_r(1, "DONE", "a"), _r(2, "STUCK", ""), _r(2, "STUCK", "")])
    assert out[1]["outcome"] == "STUCK"
    assert "未取得" in fo.aggregation_prompt("g", out)


def test_records_without_a_slice_number_are_left_alone():
    recs = [{"outcome": "DONE", "result": "x"}, _r(1, "DONE", "a")]
    assert len(fo.collapse_retries(recs)) == 2


def test_collapsing_keeps_the_slices_in_order():
    out = fo.collapse_retries([_r(3, "DONE", "c"), _r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert [r["subtask_index"] for r in out] == [1, 2, 3]
