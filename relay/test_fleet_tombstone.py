"""A job a human stopped stays stopped across a coordinator restart.

These tests reproduce the resurrection the owner saw -- a cancelled / operator-closed job
coming back after a restart -- and assert the durable tombstone
(relay/fleet_tombstone.py) blocks every start/resume/re-admission path. They use only the
tombstone module's public surface against a temp state dir; no coordinator, no network, no
real desktop. A "restart" is simulated by dropping every in-memory structure and reading the
tombstone back from disk, which is the whole point: the floor is a pure disk read.
"""

import os

import pytest

from relay import fleet_tombstone as tomb


@pytest.fixture()
def state_dir(tmp_path):
    return str(tmp_path / ".fleet")


def test_write_then_is_tombstoned_survives_a_simulated_restart(state_dir):
    # The operator cancels a job. After a "restart" (nothing in memory), the only truth is the
    # file on disk -- is_tombstoned must still say stop.
    assert tomb.is_tombstoned(state_dir, "58b5207768ec") is False
    assert tomb.write_tombstone(state_dir, "58b5207768ec", reason=tomb.REASON_CANCELLED) is True
    # Simulated restart: no shared state, re-read from disk only.
    assert tomb.is_tombstoned(state_dir, "58b5207768ec") is True
    assert tomb.tombstone_reason(state_dir, "58b5207768ec") == tomb.REASON_CANCELLED
    assert "58b5207768ec" in tomb.tombstoned_jids(state_dir)


def test_missing_dir_and_unusable_jid_are_not_tombstoned(state_dir):
    assert tomb.is_tombstoned(state_dir, "neverclosed") is False
    assert tomb.is_tombstoned(state_dir, "") is False
    assert tomb.is_tombstoned(state_dir, None) is False
    assert tomb.is_tombstoned(state_dir, "../escape") is False
    # A path-separator jid must never resolve to a real file outside tasks/closed.
    assert tomb.tombstone_path(state_dir, "a/b") == ""


def test_a_cancelled_outcome_blocks_the_resume_ledger_path(state_dir):
    # Path A in the report: the resume ledger would re-queue a goal whose done-map CANCELLED
    # never matched its key. With a tombstone keyed by jid, blocked_jid stops it regardless.
    jid = "361689fccd96"
    tomb.write_tombstone(state_dir, jid, reason=tomb.REASON_CANCELLED)
    ledger_goal = {"jid": jid, "text": "UIA A/B test; open Notepad and Calculator"}
    assert tomb.blocked_jid(state_dir, ledger_goal) == jid


def test_a_fanout_child_inherits_its_parents_tombstone_after_restart(state_dir):
    # Path A, the real bug: a fan-out child carries NO jid of its own, only the parent's
    # task_id / campaign_id / root_id. Tombstoning the parent's identities must stop the child.
    parent_jid = "58b5207768ec"
    # The close/cancel site tombstones every identity the worker carried.
    worker = {
        "jid": parent_jid,
        "task_id": parent_jid,
        "campaign_id": "camp-uia",
        "root_id": "root-uia",
    }
    ids = tomb.worker_identities(worker)
    assert parent_jid in ids and "camp-uia" in ids and "root-uia" in ids
    for _id in ids:
        tomb.write_tombstone(state_dir, _id, reason=tomb.REASON_CANCELLED)
    # Simulated restart: a child goal dict as fanout.child_goals writes it -- no jid, carries
    # parent_task_id / campaign_id / root_id.
    child = {
        "text": "type a 30-char string into Notepad and read it back",
        "parent_task_id": parent_jid,
        "campaign_id": "camp-uia",
        "root_id": "root-uia",
        "role": "subtask",
    }
    assert tomb.blocked_jid(state_dir, child) in (parent_jid, "camp-uia", "root-uia")
    # And a child carrying the explicit parent_jid field fanout now also writes.
    child2 = {"text": "press 12+30= and verify 42", "parent_jid": parent_jid}
    assert tomb.blocked_jid(state_dir, child2) == parent_jid


def test_operator_close_writes_a_tombstone_that_blocks_a_live_add(state_dir):
    # Path at the live-add / adopt door: the operator closed the worker (`close` command,
    # "手動で停止・タブ解放しました"). A later add_goal for the same jid must be refused.
    jid = "506c2633931b"
    tomb.write_tombstone(state_dir, jid, reason=tomb.REASON_OPERATOR_CLOSE)
    assert tomb.tombstone_reason(state_dir, jid) == tomb.REASON_OPERATOR_CLOSE
    add_goal = {"jid": jid, "text": "resume the UIA test", "retry": True, "priority": True}
    assert tomb.blocked_jid(state_dir, add_goal) == jid


def test_idempotent_reclose_keeps_the_first_reason_and_appends_an_event(state_dir):
    jid = "a9e2d2031457"
    tomb.write_tombstone(state_dir, jid, reason=tomb.REASON_CANCELLED, worker="w68")
    tomb.write_tombstone(state_dir, jid, reason=tomb.REASON_OPERATOR_CLOSE, worker="w68")
    # First reason wins (cancelled-then-closed audit trail), both events kept.
    assert tomb.tombstone_reason(state_dir, jid) == tomb.REASON_CANCELLED
    path = tomb.tombstone_path(state_dir, jid)
    import json
    with open(path, encoding="utf-8-sig") as fh:
        d = json.load(fh)
    reasons = [e["reason"] for e in d["events"]]
    assert reasons == [tomb.REASON_CANCELLED, tomb.REASON_OPERATOR_CLOSE]


def test_a_tombstone_is_removed_only_by_a_new_jid_never_by_a_restart(state_dir):
    jid = "01fdaeb8b066"
    tomb.write_tombstone(state_dir, jid, reason=tomb.REASON_CANCELLED)
    # A restart / resume / reaper has no new jid -> must NOT clear it.
    assert tomb.remove_tombstone(state_dir, jid) is False
    assert tomb.remove_tombstone(state_dir, jid, operator_resubmit_jid="") is False
    assert tomb.remove_tombstone(state_dir, jid, operator_resubmit_jid=jid) is False
    assert tomb.is_tombstoned(state_dir, jid) is True
    # Only an explicit operator re-submission with a DIFFERENT jid clears it.
    assert tomb.remove_tombstone(state_dir, jid, operator_resubmit_jid="newjid123") is True
    assert tomb.is_tombstoned(state_dir, jid) is False


def test_a_goal_without_a_jid_is_never_blocked_by_this_key(state_dir):
    # The tombstone is a jid rule; a legacy text-only goal has no jid and so is not blocked
    # here (the done-map still covers that path). This guards against over-blocking.
    tomb.write_tombstone(state_dir, "somejid", reason=tomb.REASON_CANCELLED)
    assert tomb.blocked_jid(state_dir, {"text": "an ordinary goal with no jid"}) is None
