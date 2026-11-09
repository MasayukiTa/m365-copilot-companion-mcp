"""A CLOSED fan-out child stays stopped across a coordinator restart.

Regression for the 2026-11-07 resurrection: worker w33 was a jid-less fan-out child whose goal
began 'WORKTREE: .../offC'. The operator closed it via .fleet/commands.d {close:[w33]}; it
showed cancelled. After main.py restarted, the SAME child goal ran again as w27 and launched
cargo builds. Its parents w1/w9/w10 had been closed earlier.

Root cause: the campaign-ledger resume path (fleet_resume.resume_children_goals) re-queues
children straight from campaigns.jsonl and never passed through the jid tombstone check that
fleet_runner._resume_goals applies to the goals ledger. A child carries no jid of its own, so
its close is recorded under its task_id (worker_identities tombstones task_id / parent_task_id
/ campaign_id / root_id), and a closed PARENT is recorded under campaign_id / root_id -- but
resume_children_goals matched none of them, so the child came back.

These tests use only the public surface of fleet_tombstone + fleet_resume against a temp state
dir. A 'restart' is simulated by writing the ledger + tombstones to disk and calling
resume_children_goals fresh, with no in-memory coordinator state -- the tombstone floor is a
pure disk read, which is the whole point.
"""

import json
import os

import pytest

from relay import fanout
from relay import fleet_resume
from relay import fleet_tombstone as tomb


@pytest.fixture()
def state_dir(tmp_path):
    d = str(tmp_path / ".fleet")
    os.makedirs(d, exist_ok=True)
    return d


def _write_campaign_ledger(state_dir, cid, parent_goal, children, *, run_id="run-1"):
    """Write a campaigns.jsonl with one header line and one line per child goal object, exactly
    the shape fanout.ledger_ingest reads (kind='campaign' header, then the child goal dicts).
    Each child line IS the goal object fanout.child_goals produced."""
    path = os.path.join(state_dir, fleet_resume.CAMPAIGNS_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        header = {"kind": "campaign", "campaign_id": cid, "goal": parent_goal,
                  "n": len(children), "cwd": None, "run_id": run_id}
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for ch in children:
            # The runner persists each child as a ledger row carrying its goal object; the
            # reader appends any non-marker, non-header row with this campaign_id to children.
            row = dict(ch)
            row["campaign_id"] = cid
            row.setdefault("subtask_index", ch.get("subtask_index"))
            # store the whole goal object under `goal` so _child_done / resume read the normal
            # (non-degraded) shape
            row["goal"] = dict(ch)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    # a fresh index per call (the module caches ledger indexes by path)
    fleet_resume._LEDGER_INDEXES.pop(path, None)
    return path


def _make_children(cid):
    steps = ["slice 1: do A", "slice 2: do B", "slice 3: do C"]
    kids = fanout.child_goals("parent goal text", steps, campaign_id=cid)
    assert kids, "fanout.child_goals produced no children"
    return kids


# --------------------------------------------------------------- child_blocked unit

def test_child_blocked_by_its_own_closed_task_id(state_dir):
    cid = "campAAAAAAAA"
    kids = _make_children(cid)
    child = kids[0]
    # The operator closed this child. worker_identities(child) is DELIBERATELY broad: besides
    # the child's own task_id it tombstones the family ids it carries (campaign_id / root_id /
    # parent_task_id) so that closing a PARENT stops its children. For a top-level split those
    # family ids ARE the campaign id, so closing one child tombstones the whole family -- the
    # same terminal-intent semantics worker_identities documents. child_blocked must return a
    # tombstoned id (its own task_id is reported first).
    for _id in tomb.worker_identities(child):
        tomb.write_tombstone(state_dir, _id, reason=tomb.REASON_OPERATOR_CLOSE)
    assert tomb.child_blocked(state_dir, {"goal": child}, cid) == child["task_id"]
    # Siblings share the family campaign id, so they are blocked by it too (family-terminal).
    assert tomb.child_blocked(state_dir, {"goal": kids[1]}, cid) == cid


def test_child_blocked_when_the_parent_campaign_is_closed(state_dir):
    cid = "campBBBBBBBB"
    kids = _make_children(cid)
    # The operator closed the PARENT. Its worker carried the campaign id; worker_identities
    # tombstoned it. Every child repeats that campaign_id, so all children must be blocked.
    parent_worker = {"jid": "parentjid01", "task_id": "parentjid01", "campaign_id": cid,
                     "root_id": cid}
    for _id in tomb.worker_identities(parent_worker):
        tomb.write_tombstone(state_dir, _id, reason=tomb.REASON_OPERATOR_CLOSE)
    for ch in kids:
        blk = tomb.child_blocked(state_dir, {"goal": ch}, cid)
        assert blk == cid, (ch.get("task_id"), blk)


def test_child_blocked_handles_the_degraded_flat_row(state_dir):
    # A pre-`goal`-object ledger line: the id fields are on the flat row, not under `goal`.
    cid = "campCCCCCCCC"
    tomb.write_tombstone(state_dir, cid + "-2", reason=tomb.REASON_OPERATOR_CLOSE)
    flat = {"text": "slice 2 only", "task_id": cid + "-2", "campaign_id": cid,
            "subtask_index": 2}
    assert tomb.child_blocked(state_dir, flat, cid) == cid + "-2"


def test_child_not_blocked_when_nothing_is_closed(state_dir):
    cid = "campDDDDDDDD"
    kids = _make_children(cid)
    for ch in kids:
        assert tomb.child_blocked(state_dir, {"goal": ch}, cid) is None


# --------------------------------------------------- resume integration (the reported bug)

def test_closed_jidless_child_is_not_resumed_after_restart(state_dir):
    """The exact incident: close a jid-less child, simulate a restart, assert resume does not
    re-queue it, while its unclosed siblings still resume."""
    cid = "campEEEEEEEE"
    kids = _make_children(cid)
    closed_child = kids[0]
    _write_campaign_ledger(state_dir, cid, "parent goal text", kids)

    # Operator closes the jid-less child (the close site tombstones every identity it carries).
    for _id in tomb.worker_identities(closed_child):
        tomb.write_tombstone(state_dir, _id, reason=tomb.REASON_OPERATOR_CLOSE,
                             worker="w33")
    assert closed_child["task_id"] in tomb.tombstoned_jids(state_dir)

    # Simulated restart: no in-memory state. Resume reads the ledger + tombstones from disk.
    # Pass scope explicitly so the test does not depend on interrupted_run_scope heuristics.
    goals, _degraded = fleet_resume.resume_children_goals(
        state_dir, done_map={}, log=lambda *a, **k: None, scope={cid})
    requeued_tasks = {g.get("task_id") for g in goals}

    # THE CORE ASSERTION: the closed jid-less child is NOT resumed after the restart. This is
    # the regression -- before the fix it came back as a new worker (w33 -> w27) and relaunched
    # its build. For a top-level split, worker_identities also tombstones the shared family id,
    # so the whole family stays down (terminal intent); that matches the incident, where the
    # parents w1/w9/w10 had themselves been closed. The point of this test is only that the
    # closed child never reappears.
    assert closed_child["task_id"] not in requeued_tasks, \
        "a closed jid-less child was resumed after a restart"

    # The child's own tombstone is present (so the next resume is O(1) and auditable).
    assert tomb.is_tombstoned(state_dir, closed_child["task_id"]) is True


def test_closed_parent_stops_every_child_on_resume(state_dir):
    """Closing the PARENT (campaign) must stop ALL its children on resume, not just one."""
    cid = "campFFFFFFFF"
    kids = _make_children(cid)
    _write_campaign_ledger(state_dir, cid, "parent goal text", kids)

    parent_worker = {"jid": "parentjidFF", "task_id": "parentjidFF", "campaign_id": cid,
                     "root_id": cid}
    for _id in tomb.worker_identities(parent_worker):
        tomb.write_tombstone(state_dir, _id, reason=tomb.REASON_OPERATOR_CLOSE)

    goals, _degraded = fleet_resume.resume_children_goals(
        state_dir, done_map={}, log=lambda *a, **k: None, scope={cid})
    assert goals == [], "a child of a closed parent campaign was resumed"


def test_unclosed_campaign_still_resumes_its_children(state_dir):
    """Over-blocking guard: with no tombstone, every unfinished child is still re-queued."""
    cid = "campGGGGGGGG"
    kids = _make_children(cid)
    _write_campaign_ledger(state_dir, cid, "parent goal text", kids)
    goals, _degraded = fleet_resume.resume_children_goals(
        state_dir, done_map={}, log=lambda *a, **k: None, scope={cid})
    requeued = {g.get("task_id") for g in goals}
    for ch in kids:
        assert ch["task_id"] in requeued
