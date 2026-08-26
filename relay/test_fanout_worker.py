"""The worker half of fan-out: what a split reply does to the worker that produced it.

relay/test_fanout.py fixes the RULES (what counts as a split, what a child inherits, when a
campaign is finished). These fix the BEHAVIOUR around them -- which is where a fan-out can go
wrong in ways the rules cannot see: a parent that keeps its slot while its children wait for
one, a split reply mistaken for a finished task, a goal stranded because the agent divided it
badly.
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


def test_a_fanout_worker_opens_by_asking_for_a_split_not_for_the_work():
    w = _worker()
    assert fo.SUBTASKS_READY in w.job
    assert "分割" in w.job


def test_the_split_request_still_carries_the_whole_goal():
    """An agent asked to divide a goal it cannot see divides something else."""
    w = _worker(goal="社内一斉配信は除外し、日付/差出人/件名の形式で出力すること")
    assert "社内一斉配信は除外" in w.job


def test_fanout_is_off_for_a_child_however_it_is_constructed():
    """Structural, not a promise: recursive splitting is how one goal becomes unbounded."""
    w = _worker(fanout=True, depth=1)
    assert w.fanout is False
    assert fo.SUBTASKS_READY not in w.job


def test_an_ordinary_worker_is_untouched():
    w = _worker(fanout=False)
    assert w.fanout is False
    assert fo.SUBTASKS_READY not in w.job


# ---- the split reply ----------------------------------------------------------------------

def test_a_split_spawns_the_children_and_ENDS_the_parent():
    """The parent must let go of its slot. Parked until its children finish, with a
    concurrency cap below the number of children, it waits for children that cannot start."""
    got = []
    w = _worker(spawn=lambda goal, kids: got.append((goal, kids)))
    w._decide(SPLIT_REPLY)

    assert len(got) == 1
    parent_goal, kids = got[0]
    assert parent_goal == w.goal
    assert len(kids) == 3
    assert w.status in rf.TERMINAL, "a parent that stays alive holds a slot its children need"
    assert w.outcome == "FANOUT"
    assert "3 個" in w.reason


def test_the_children_carry_the_parents_goal_and_their_own_slice():
    got = []
    w = _worker(spawn=lambda goal, kids: got.append(kids))
    w._decide(SPLIT_REPLY)
    kids = got[0]
    assert all(w.goal in k["text"] for k in kids)
    assert "2026年1月分" in kids[0]["text"]
    assert "2026年3月分" in kids[2]["text"]
    assert [k["depth"] for k in kids] == [1, 1, 1]


def test_a_split_reply_is_not_accepted_as_a_finished_task():
    """It ends in a marker and lists intentions. Read by the DONE branch first, a plan to do
    the work would be filed as the work."""
    reply = SPLIT_REPLY + "\nDONE"
    got = []
    w = _worker(spawn=lambda goal, kids: got.append(kids))
    w._decide(reply)
    assert w.outcome == "FANOUT"
    assert len(got) == 1


def test_an_unusable_split_falls_back_to_doing_the_work_here():
    """Refusing to proceed would strand the goal because the agent divided it badly."""
    w = _worker(spawn=lambda goal, kids: pytest.fail("must not spawn"))
    w._decide("1. 全部やる\n%s" % fo.SUBTASKS_READY)
    assert w.status not in rf.TERMINAL
    assert w.fanout is False
    assert "直接実行" in w.job


def test_a_reply_without_the_marker_is_asked_for_the_marker():
    """Without it there is nothing to tell a finished list from a half-written one."""
    w = _worker(spawn=lambda goal, kids: pytest.fail("must not spawn"))
    w._decide("分割案を検討しています。1. 1月分\n2. 2月分")
    assert w.status == "ready"
    assert fo.SUBTASKS_READY in w.job


def test_the_split_happens_once():
    """A second split reply must not produce a second family."""
    got = []
    w = _worker(spawn=lambda goal, kids: got.append(kids))
    w._decide(SPLIT_REPLY)
    w.status = "waiting"          # pretend the fleet revived it
    w._decide(SPLIT_REPLY)
    assert len(got) == 1


def test_with_no_spawner_the_worker_does_the_job_itself():
    """A caller that enabled fan-out without wiring the queue gets the work done, not a
    goal silently dropped on the floor."""
    w = _worker(spawn=None)
    w._decide(SPLIT_REPLY)
    assert w.status not in rf.TERMINAL
    assert w.fanout is False


# ---- when the merge gets queued -----------------------------------------------------------
#
# The bug these pin cost a whole real run. The split fired, nine subtasks ran, every one of
# them finished -- and the run ended without ever writing the answer they were collected for.
# The merge was decided at the TOP of the sweep body, and the last child finishes further
# down the SAME body, so the pass that would have noticed was the pass the loop exited on.
# There was no pass in which "the family is finished" could be observed.

class _FakeWorker:
    """Only what the merge check reads off a worker."""

    def __init__(self, cid, index, status, outcome="DONE", result="ok"):
        self.task_envelope = type("E", (), {"campaign_id": cid, "role": "subtask"})()
        self.subtask_index = index
        self.status = status
        self.outcome = outcome
        self.display_result = result
        self.last_response = result


def _merge_state(children, n=3):
    """Reproduce the loop's decision with the same inputs run_fleet gives it."""
    cid = "c-test"
    campaigns = {cid: {"goal": "元の目標", "n": n, "merged": False}}
    add_box = []
    workers = children

    def queue_ready():
        queued = 0
        for _cid, _camp in campaigns.items():
            if _camp.get("merged"):
                continue
            kids = [w for w in workers
                    if getattr(getattr(w, "task_envelope", None), "campaign_id", "") == _cid
                    and getattr(getattr(w, "task_envelope", None), "role", "") == "subtask"]
            recs = [{"finished": w.status in rf.TERMINAL, "outcome": w.outcome,
                     "subtask_index": w.subtask_index,
                     "result": w.display_result} for w in kids]
            if len(recs) < _camp.get("n", 0) or not fo.ready_to_aggregate(recs):
                continue
            _camp["merged"] = True
            add_box.append(fo.aggregation_goal(_camp["goal"], recs, campaign_id=_cid))
            queued += 1
        return queued

    return queue_ready, add_box


def test_a_family_still_working_is_not_merged():
    kids = [_FakeWorker("c-test", 1, "done"), _FakeWorker("c-test", 2, "waiting"),
            _FakeWorker("c-test", 3, "done")]
    queue_ready, add_box = _merge_state(kids)
    assert queue_ready() == 0
    assert add_box == []


def test_the_merge_is_queued_the_moment_the_last_child_finishes():
    kids = [_FakeWorker("c-test", 1, "done"), _FakeWorker("c-test", 2, "waiting"),
            _FakeWorker("c-test", 3, "done")]
    queue_ready, add_box = _merge_state(kids)
    assert queue_ready() == 0
    kids[1].status = "done"                     # the last child finishes
    assert queue_ready() == 1, "the pass after the last child finishes must queue the merge"
    assert len(add_box) == 1
    assert add_box[0]["role"] == "aggregator"


def test_the_merge_is_queued_exactly_once():
    """The loop condition calls this on every pass; a second merge would double the work."""
    kids = [_FakeWorker("c-test", i, "done") for i in (1, 2, 3)]
    queue_ready, add_box = _merge_state(kids)
    assert queue_ready() == 1
    assert queue_ready() == 0
    assert len(add_box) == 1


def test_a_family_whose_children_were_never_all_admitted_is_not_merged():
    """Merging a family half of which never ran would report a sweep that did not happen."""
    kids = [_FakeWorker("c-test", 1, "done"), _FakeWorker("c-test", 2, "done")]
    queue_ready, add_box = _merge_state(kids, n=3)   # three were spawned, two exist
    assert queue_ready() == 0


def test_a_failed_child_still_lets_the_family_merge_and_is_named():
    """A stuck subtask must not strand the whole campaign -- but must not be hidden either."""
    kids = [_FakeWorker("c-test", 1, "done"),
            _FakeWorker("c-test", 2, "stuck", outcome="STUCK", result=""),
            _FakeWorker("c-test", 3, "done")]
    queue_ready, add_box = _merge_state(kids)
    assert queue_ready() == 1
    assert "未取得" in add_box[0]["text"]


def test_the_loop_condition_asks_for_pending_merges():
    """Structural: the run must not be able to end with a merge owed.

    Checked against the source because the defect was one of PLACEMENT -- the code was
    correct and ran where it could never observe the state it was looking for.
    """
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "relay", "relay_fleet.py"), encoding="utf-8").read()
    head = src[src.index("    _reap_counter = 0"):]
    condition = head[:head.index("):") + 2]
    assert "_queue_ready_merges() > 0" in condition, \
        "the sweep loop must ask whether a merge is owed before it ends"


# ---- what a slow turn reports -----------------------------------------------------------

def test_a_socket_turn_reports_the_deadline_that_applies_to_it():
    """_defer_generation deliberately skips the tab-era budget for a socket turn, which has
    its own turn_timeout_s -- but the line printed the skipped number anyway, so a healthy
    turn read as 'wait 594s/360s': correct behaviour, reported as a breach."""
    import re
    src = open(rf.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    i = src.index('previous turn still generating -> wait %ds/%ds')
    window = src[max(0, i - 700):i + 200]
    assert "SOCKET_TURN_TIMEOUT_S if getattr(self, \"socket\", False)" in window, \
        "the reported bound must be the socket's own deadline when on a socket"


# ---- the answer that reaches the caller ----------------------------------------------------

class _EndWorker:
    """A worker as run_fleet holds it at the end of a run."""

    def __init__(self, goal, outcome, role="", campaign="", result="", name="w0"):
        self.goal = goal
        self.outcome = outcome
        self.status = "done"
        self.name = name
        self.reason = "r"
        self.last_response = result
        self.display_result = result
        self.task_envelope = type("E", (), {"role": role, "campaign_id": campaign,
                                            "task_id": "", "parent_task_id": None,
                                            "depth": 0})()


def _publish(workers):
    """The publishing step run_fleet performs before it returns."""
    for _w in workers:
        if (_w.outcome or "") != "FANOUT":
            continue
        cid = fo.campaign_id_for(_w.goal)
        agg = next((x for x in workers
                    if getattr(getattr(x, "task_envelope", None), "role", "") == "aggregator"
                    and getattr(getattr(x, "task_envelope", None), "campaign_id", "") == cid),
                   None)
        if agg is None:
            continue
        merged = agg.display_result or agg.last_response
        if merged:
            _w.last_response = merged
            _w.display_result = merged
            _w.reason = "%s / 統合結果を掲載 (統合ワーカー %s: %s)" % (
                _w.reason, agg.name, agg.outcome or agg.status)
    return workers


def test_the_submitted_goal_comes_back_with_the_merged_answer():
    """The caller keys results by goal TEXT, so the goal the user submitted was coming back
    carrying the parent's split proposal while the merge sat under a text nobody looks up."""
    goal = "1〜3月のメールを一覧化する"
    cid = fo.campaign_id_for(goal)
    parent = _EndWorker(goal, "FANOUT", result="分割案: 1. …")
    agg = _EndWorker("merge prompt", "DONE", role="aggregator", campaign=cid,
                     result="統合済み一覧: 全318件", name="w9")
    _publish([parent, agg])
    assert "統合済み一覧: 全318件" in parent.display_result
    assert "統合ワーカー w9" in parent.reason


def test_the_parent_keeps_its_own_outcome():
    """Only the text moves. Claiming the parent did the work hides where it was done."""
    goal = "g"
    parent = _EndWorker(goal, "FANOUT", result="split")
    agg = _EndWorker("m", "DONE", role="aggregator", campaign=fo.campaign_id_for(goal),
                     result="merged")
    _publish([parent, agg])
    assert parent.outcome == "FANOUT"


def test_a_campaign_with_no_merge_is_left_honest():
    """No merge means no merged answer; inventing one would report work never done."""
    parent = _EndWorker("g", "FANOUT", result="split proposal")
    _publish([parent])
    assert parent.display_result == "split proposal"


def test_an_empty_merge_does_not_blank_the_parent():
    goal = "g"
    parent = _EndWorker(goal, "FANOUT", result="split proposal")
    agg = _EndWorker("m", "STUCK", role="aggregator", campaign=fo.campaign_id_for(goal),
                     result="")
    _publish([parent, agg])
    assert parent.display_result == "split proposal"


def test_another_campaigns_merge_is_not_borrowed():
    parent = _EndWorker("goal A", "FANOUT", result="split A")
    agg = _EndWorker("m", "DONE", role="aggregator",
                     campaign=fo.campaign_id_for("goal B"), result="merged B")
    _publish([parent, agg])
    assert parent.display_result == "split A"
