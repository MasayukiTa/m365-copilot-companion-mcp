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


def test_trailing_dependent_merge_is_not_launched_as_a_parallel_child():
    body = (
        "1. Review slides 1-5 and save partial_1.md\n"
        "2. Review slides 6-10 and save partial_2.md\n"
        "3. Review slides 11-15 and save partial_3.md\n"
        "4. Review slides 16-20 and save partial_4.md\n"
        "5. Read the outputs from subtasks 1-4 and merge them into final.md\n"
        + READY
    )
    assert fo.subtasks_from(body) == [
        "Review slides 1-5 and save partial_1.md",
        "Review slides 6-10 and save partial_2.md",
        "Review slides 11-15 and save partial_3.md",
        "Review slides 16-20 and save partial_4.md",
    ]


def test_observed_japanese_tail_aggregator_is_removed():
    body = (
        "1. S1〜S5をレビューし _partial_S01-05.md に保存\n"
        "2. S6〜S10をレビューし _partial_S06-10.md に保存\n"
        "3. S11〜S15をレビューし _partial_S11-15.md に保存\n"
        "4. S16〜S20をレビューし _partial_S16-20.md に保存\n"
        "5. サブタスク1〜4が生成した4ファイルを読み込み、S1〜S20を統合して最終ファイルに保存\n"
        + READY
    )
    got = fo.subtasks_from(body)
    assert len(got) == 4
    assert all("統合" not in step for step in got)
    assert got[-1].startswith("S16〜S20")


def test_a_dependency_in_the_middle_refuses_the_whole_split():
    body = (
        "1. Collect January records\n"
        "2. Read subtask 1 results and validate them\n"
        "3. Collect March records\n"
        + READY
    )
    assert fo.subtasks_from(body) == []


def test_self_reference_does_not_count_as_a_cross_subtask_dependency():
    body = (
        "1. Subtask 1: collect January records\n"
        "2. Subtask 2: collect February records\n"
        + READY
    )
    assert len(fo.subtasks_from(body)) == 2


def test_split_prompt_forbids_a_parallel_merge_child():
    assert "Do not add a merge/aggregation subtask" in fo.SPLIT_JOB


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




def test_nested_campaign_id_is_scoped_by_the_splitting_task_identity():
    root = fo.campaign_id_for("same text")
    # Root compatibility: old persisted campaigns still resolve exactly as before.
    assert root == fo.campaign_id_for("same text", parent_task_id="")
    a = fo.campaign_id_for("same text", parent_task_id="outer-c1")
    b = fo.campaign_id_for("same text", parent_task_id="outer-c2")
    assert a != b != root
    assert a == fo.campaign_id_for("same text", parent_task_id="outer-c1")


def test_nested_children_get_a_family_unique_to_their_parent_task():
    steps = ["slice A collect records", "slice B collect records"]
    a = fo.child_goals("same nested goal", steps, parent_task_id="outer-c1")
    b = fo.child_goals("same nested goal", steps, parent_task_id="outer-c2")
    assert a and b
    assert a[0]["campaign_id"] != b[0]["campaign_id"]
    assert {k["parent_task_id"] for k in a} == {"outer-c1"}
    assert {k["parent_task_id"] for k in b} == {"outer-c2"}

def test_the_campaign_id_is_derived_from_the_goal_so_a_resume_rejoins_the_family():
    a = fo.child_goals("同じ目標", ["範囲A を取得する", "範囲B を取得する"])
    b = fo.child_goals("同じ目標", ["範囲A を取得する", "範囲B を取得する"])
    assert a[0]["campaign_id"] == b[0]["campaign_id"]
    c = fo.child_goals("別の目標", ["範囲A を取得する", "範囲B を取得する"])
    assert c[0]["campaign_id"] != a[0]["campaign_id"]


def test_children_do_not_split_again(monkeypatch):
    """Recursive splitting is how one runaway goal becomes an unbounded number of chats."""
    monkeypatch.setattr(fo, "hierarchical_merge_setting", lambda: "off")   # default is now on; this pins the depth-1 cap
    assert fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"],
                          depth=fo.MAX_DEPTH) == []


def test_a_child_inherits_the_cwd_but_NOT_the_whole_goals_check():
    """子は作業ディレクトリを継ぐが、**親の受入検査は継がない**。

    以前はこの検査が「継ぐこと」を要求していた。それが欠陥だった。計測(2026-09-13):
    親の検査 `{"type":"pytest","args":"-q tests/"}` を3分割すると3子とも同一の検査を持ち、
    一方で子のプロンプトは「他の範囲は別の会話が並行して担当しているので、手を出さないこと」
    と指示している。厳しい検査なら兄弟が終わるまで永久に通らず、緩い検査(file_exists 等)なら
    兄弟が作った成果物で**何もしていない子まで通る**（さらに `_salvage_via_checks` が
    それを salvaged DONE に昇格させる）。

    親の検査は「目標全体が成功したか」を問うもので、それに答えられるのは統合ワーカーだけ。
    そこへ回す (aggregation_goal の parent_checks)。引数は残さず削除した -- 黙って無視すると
    既存の呼び出し側は子が検証され続けていると思い込む。
    """
    kids = fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"], cwd="C:/x")
    assert kids[0]["cwd"] == "C:/x"
    assert not kids[0].get("checks"), "子が全体目標の検査を背負っている"

    with pytest.raises(TypeError):
        fo.child_goals("親", ["範囲A を取得する", "範囲B を取得する"],
                       checks=[{"type": "pytest"}])


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
    assert "そのまま並べ" in p
    assert "DONE と書いてください" in p


def test_the_merge_is_not_asked_to_reemit_everything():
    """The merge must not inherit the disease fan-out cures. Asked for "the final answer",
    an agent holding eight reports of hundreds of rows tries to write them all out again --
    the size problem, arriving at the last step. The first live merge ran fourteen turns and
    went STUCK having produced nothing."""
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert "全件を1つの応答に書き出そうとしないでください" in p
    assert "ファイル" in p, "an oversized table needs somewhere to go other than the reply"


def test_the_merge_is_asked_for_coverage_and_gaps():
    p = fo.aggregation_prompt("元の目標", [_r(1, "DONE", "a"), _r(2, "DONE", "b")])
    assert "取得件数" in p
    assert "未取得" in p


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


def test_nested_merge_fallback_uses_the_same_parent_scoped_campaign_as_children():
    steps = ["slice A collect records", "slice B collect records"]
    kids = fo.child_goals("same nested goal", steps, parent_task_id="outer-c1")
    merge = fo.aggregation_goal("same nested goal", [_r(1, "DONE", "a")],
                                parent_task_id="outer-c1")
    assert kids
    assert merge["campaign_id"] == kids[0]["campaign_id"]
    assert merge["parent_task_id"] == "outer-c1"


def test_nested_merge_fallback_changes_with_parent_task_identity():
    a = fo.aggregation_goal("same nested goal", [_r(1, "DONE", "a")],
                            parent_task_id="outer-c1")
    b = fo.aggregation_goal("same nested goal", [_r(1, "DONE", "a")],
                            parent_task_id="outer-c2")
    assert a["campaign_id"] != b["campaign_id"]


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


# ---- a candidate is not a retry ----------------------------------------------------------

def _cand(idx, outcome, cand=None, result=""):
    rec = {"subtask_index": idx, "outcome": outcome, "result": result, "finished": True}
    if cand is not None:
        rec["candidate_index"] = cand
    return rec


def test_best_of_n_candidates_all_survive_the_collapse():
    """THE MEASURED DEFECT. best-of-N runs one goal N times on purpose, so N workers carry the
    same slice number -- and this function read that as a family retried N-1 times. Three DONE
    records went in and one came out, the first. The candidates never reached the selector,
    which is the entire mechanism, and nothing in the output said any had been dropped."""
    out = fo.collapse_retries([_cand(1, "DONE", 1, "a"), _cand(1, "DONE", 2, "b"),
                               _cand(1, "DONE", 3, "c")])
    assert len(out) == 3
    assert sorted(r["result"] for r in out) == ["a", "b", "c"]


def test_a_retry_of_one_candidate_still_collapses():
    """The rule was never wrong -- the relationships are different. A retry REPLACES the
    attempt before it; a candidate SITS BESIDE it."""
    out = fo.collapse_retries([_cand(1, "STUCK", 2), _cand(1, "DONE", 2, "won"),
                               _cand(1, "DONE", 1, "other")])
    assert len(out) == 2
    assert {r.get("candidate_index") for r in out} == {1, 2}
    assert [r["result"] for r in out if r["candidate_index"] == 2] == ["won"]


def test_records_without_a_candidate_number_behave_exactly_as_before():
    """Every record written before this existed has no candidate number, and their collapsing
    must not change by a single case."""
    out = fo.collapse_retries([_cand(1, "STUCK"), _cand(1, "DONE", result="fixed")])
    assert len(out) == 1 and out[0]["result"] == "fixed"


def test_candidates_and_plain_slices_coexist():
    out = fo.collapse_retries([_cand(1, "DONE", 1), _cand(1, "DONE", 2), _cand(2, "DONE")])
    assert len(out) == 3
    assert [r["subtask_index"] for r in out] == [1, 1, 2]


def test_the_order_is_stable_across_slices_and_candidates():
    """A family has to read the same way every time, or a merge prompt built from it changes
    for reasons that have nothing to do with the work."""
    out = fo.collapse_retries([_cand(2, "DONE", 2), _cand(1, "DONE", 2),
                               _cand(2, "DONE", 1), _cand(1, "DONE", 1)])
    assert [(r["subtask_index"], r["candidate_index"]) for r in out] == [
        (1, 1), (1, 2), (2, 1), (2, 2)]
