"""Closing a worker must stop its fan-out children that are ALREADY RUNNING, not only
block their next start.

The durable tombstone (test_fleet_tombstone.py) already covers the START/RESUME/ADOPT
doors: a fan-out child of a closed parent is refused there because it inherits the
parent's task_id / campaign_id / root_id. But an in-flight child has already passed that
door, and nothing re-checks the tombstone mid-run -- so it keeps going. That is exactly
what happened on 2026-xx: a `close` cancelled w0..w10, yet w33 (goal starting
'WORKTREE: .../agents/offC', NO jid, carrying the closed parent's fan-out ids) kept
running until it was closed by name 46 minutes later, after launching `cargo test`.

These tests pin the LIVE counterpart to the tombstone: fleet_tombstone.descendants_to_cancel
selects, by pure in-memory id-matching (no disk, no coordinator, no network), the running
workers that inherit a closed worker's identities and must therefore be cancelled now.
"""

from relay import fleet_tombstone as tomb


class _FakeWorker:
    """The attributes descendants_to_cancel reads off a live worker: name, status, the
    fan-out ids, and .cancel(). A fan-out child has NO jid of its own."""

    def __init__(self, name, status="running", *, jid=None, task_id=None,
                 parent_task_id=None, campaign_id=None, root_id=None):
        self.name = name
        self.status = status
        self.jid = jid
        self.task_id = task_id
        self.parent_task_id = parent_task_id
        self.campaign_id = campaign_id
        self.root_id = root_id
        self.task_envelope = None
        self.cancelled = False

    def get(self, k):  # never used (not a dict), but keeps duck-typing honest
        return getattr(self, k, None)

    def cancel(self):
        self.cancelled = True
        self.status = "cancelled"


def _closed_ids(*workers):
    """Union of worker_identities over the workers named in a close command -- the exact
    value the close site accumulates before calling descendants_to_cancel."""
    ids = []
    for w in workers:
        ids.extend(tomb.worker_identities(w))
    return ids


def _select(closed_workers, candidates):
    """Call descendants_to_cancel exactly as the close site does: closed identities are the
    union over the NAMED workers, and those same worker objects are passed as closed_workers
    so they are excluded from the result."""
    return tomb.descendants_to_cancel(
        _closed_ids(*closed_workers), candidates, closed_workers=closed_workers)


def test_running_child_of_a_closed_parent_is_selected_for_cancel():
    # The w33 incident, minimised. Parent w0 fans out; child w33 carries the parent's
    # campaign/root/parent_task ids and has no jid. Closing w0 must select w33.
    parent = _FakeWorker("w0", jid="parent0", task_id="task0",
                         campaign_id="camp-offC", root_id="root-offC")
    child = _FakeWorker("w33", parent_task_id="task0",
                        campaign_id="camp-offC", root_id="root-offC")
    unrelated = _FakeWorker("w7", jid="other", campaign_id="camp-else",
                            root_id="root-else")

    to_cancel = _select([parent], [parent, child, unrelated])

    names = [w.name for w in to_cancel]
    assert "w33" in names                 # the running descendant is caught
    assert "w7" not in names              # an unrelated worker is not
    assert "w0" not in names              # the parent itself was already cancelled by the loop


def test_a_grandchild_two_levels_down_is_still_caught_by_shared_root():
    # Fan-out can nest: a grandchild carries the root_id of the top ancestor. Closing the
    # top worker must still reach it, because inheritance runs through the shared ids.
    top = _FakeWorker("w0", jid="top", task_id="task-top",
                      campaign_id="camp", root_id="root-top")
    grandchild = _FakeWorker("w41", parent_task_id="task-mid", root_id="root-top")
    to_cancel = _select([top], [top, grandchild])
    assert [w.name for w in to_cancel] == ["w41"]


def test_a_terminal_descendant_is_still_returned_but_the_caller_skips_it():
    # descendants_to_cancel is a pure selector and does not read status to decide membership
    # (the close site does the TERMINAL guard before calling .cancel()). A child that is
    # already done is still matched by id; asserting this keeps the contract explicit so the
    # status guard stays at the call site, where cancel() actually happens.
    parent = _FakeWorker("w0", jid="p", campaign_id="camp-x", root_id="root-x")
    done_child = _FakeWorker("w9", status="done", parent_task_id="p",
                             campaign_id="camp-x", root_id="root-x")
    out = _select([parent], [parent, done_child])
    assert [w.name for w in out] == ["w9"]


def test_no_closed_identities_selects_nothing():
    # A close of a plain worker that carries no ids yields an empty closed set; nothing is
    # cancelled. Guards against over-cancelling when there is no fan-out at all.
    plain = _FakeWorker("w0")   # no jid, no fan-out ids
    others = [_FakeWorker("w1", jid="a"), _FakeWorker("w2", campaign_id="c")]
    assert _select([plain], [plain] + others) == []
    assert tomb.descendants_to_cancel([], others) == []


def test_a_sibling_sharing_only_a_different_campaign_is_not_cancelled():
    # Over-blocking guard: two campaigns running at once. Closing a worker in campaign A
    # must not touch a worker whose only ids are in campaign B.
    a_parent = _FakeWorker("w0", jid="pa", campaign_id="camp-A", root_id="root-A")
    b_child = _FakeWorker("w5", parent_task_id="pb", campaign_id="camp-B",
                          root_id="root-B")
    out = _select([a_parent], [a_parent, b_child])
    assert out == []


def test_each_descendant_is_returned_once_even_if_many_ids_overlap():
    # A child can share the parent's campaign AND root AND parent_task at once; it must
    # appear exactly once in the result, not once per overlapping id.
    parent = _FakeWorker("w0", jid="p", task_id="t", campaign_id="camp",
                         root_id="root")
    child = _FakeWorker("w33", parent_task_id="t", campaign_id="camp",
                        root_id="root")
    out = _select([parent], [parent, child, child])
    assert [w.name for w in out] == ["w33"]


def test_goal_or_worker_ids_unions_own_and_inherited():
    # The matching helper exposes both a worker's OWN identities and the ancestor ids it
    # inherits through, de-duplicated -- the disk-free sibling of blocked_jid.
    child = _FakeWorker("w33", parent_task_id="task0", campaign_id="camp-offC",
                        root_id="root-offC")
    ids = set(tomb.goal_or_worker_ids(child))
    assert {"task0", "camp-offC", "root-offC"} <= ids
