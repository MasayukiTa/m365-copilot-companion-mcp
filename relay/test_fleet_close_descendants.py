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


def test_closing_the_parent_still_cancels_child_and_grandchild():
    # Closing the TOP of a fan-out tree must reach every live descendant, however deep. The
    # grandchild names only its immediate parent (task-mid), so the selector has to WALK the
    # chain: top -> child(task-mid) -> grandchild, not rely on a shared root_id (which would
    # also sweep in siblings and the parent -- the bug this PR fixes).
    top = _FakeWorker("w0", jid="top", task_id="task-top",
                      campaign_id="camp", root_id="root-top")
    child = _FakeWorker("w10", task_id="task-mid", parent_task_id="task-top",
                        campaign_id="camp", root_id="root-top")
    grandchild = _FakeWorker("w41", parent_task_id="task-mid",
                             campaign_id="camp", root_id="root-top")
    to_cancel = _select([top], [top, child, grandchild])
    assert sorted(w.name for w in to_cancel) == ["w10", "w41"]


def test_closing_one_child_does_not_cancel_its_sibling_or_its_parent():
    # THE REVIEW FINDING. Siblings and the parent carry the SAME campaign_id/root_id as the
    # closed child, so matching on a shared group id would cancel the whole tree from closing
    # ONE child. Closing a child must cancel ONLY that child's own descendants -- here it has
    # none -- and must leave its sibling and its parent running.
    parent = _FakeWorker("w0", jid="parent0", task_id="task0",
                         campaign_id="camp", root_id="root")
    child_a = _FakeWorker("w1", task_id="task-a", parent_task_id="task0",
                          campaign_id="camp", root_id="root")
    child_b = _FakeWorker("w2", task_id="task-b", parent_task_id="task0",
                          campaign_id="camp", root_id="root")
    out = _select([child_a], [parent, child_a, child_b])
    names = [w.name for w in out]
    assert "w2" not in names     # the SIBLING is not cancelled
    assert "w0" not in names     # the PARENT is not cancelled
    assert out == []             # child_a has no descendants of its own


def test_closing_a_child_cancels_only_its_own_grandchild_not_the_tree():
    # Closing a MIDDLE worker cancels its own subtree (its grandchild) but still spares its
    # sibling and its parent, even though all four share campaign/root.
    parent = _FakeWorker("w0", jid="parent0", task_id="task0",
                         campaign_id="camp", root_id="root")
    child = _FakeWorker("w1", task_id="task-a", parent_task_id="task0",
                        campaign_id="camp", root_id="root")
    sibling = _FakeWorker("w2", task_id="task-b", parent_task_id="task0",
                          campaign_id="camp", root_id="root")
    grandchild = _FakeWorker("w9", parent_task_id="task-a",
                             campaign_id="camp", root_id="root")
    out = _select([child], [parent, child, sibling, grandchild])
    names = [w.name for w in out]
    assert names == ["w9"]       # only child's own descendant
    assert "w2" not in names     # sibling spared
    assert "w0" not in names     # parent spared


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
