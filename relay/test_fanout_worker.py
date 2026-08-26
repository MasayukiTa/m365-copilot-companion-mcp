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
