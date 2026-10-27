# -*- coding: utf-8 -*-
"""A manual top-priority boost on a parent task reaches its split/campaign children.

These are rules about the PURE propagation function in ui.priority_propagation: given the
directly-boosted task ids and the fleet's worker rows (the same task_id / parent_task_id /
campaign_id rows relay.task_tree reads), which ids are boosted once the boost flows down.
Nothing here touches a file, the cockpit, or a worker row.
"""
from __future__ import annotations

from ui.priority_propagation import (
    DEFAULT_FLAG_FIELD,
    boosted_ids_from_rows,
    effective_boosted_ids,
    is_boosted,
    propagate_boost,
)


def _parent(task_id, campaign_id, **kw):
    row = {"name": "p", "task_id": task_id, "campaign_id": campaign_id,
           "parent_task_id": None, "role": "producer", "depth": 0}
    row.update(kw)
    return row


def _child(task_id, parent_task_id, campaign_id, idx, **kw):
    row = {"name": "k%d" % idx, "task_id": task_id, "parent_task_id": parent_task_id,
           "campaign_id": campaign_id, "role": "subtask", "depth": 1, "subtask_index": idx}
    row.update(kw)
    return row


def _family():
    """One parent (t-parent / campaign c1) with three subtask children."""
    p = _parent("t-parent", "c1")
    kids = [_child("c1-%d" % i, "t-parent", "c1", i) for i in (1, 2, 3)]
    return p, kids


def test_boosting_the_parent_boosts_every_child():
    p, kids = _family()
    rows = [p] + kids
    eff = effective_boosted_ids({"t-parent"}, rows)
    assert eff == {"t-parent", "c1-1", "c1-2", "c1-3"}


def test_boosting_one_child_does_not_boost_its_siblings_or_parent():
    p, kids = _family()
    rows = [p] + kids
    eff = effective_boosted_ids({"c1-2"}, rows)
    assert eff == {"c1-2"}
    assert "t-parent" not in eff
    assert "c1-1" not in eff and "c1-3" not in eff


def test_a_deep_subtree_is_boosted_to_the_bottom():
    # t-parent -> c1-1 -> (g1, g2); boosting t-parent reaches the grandchildren.
    p, kids = _family()
    g1 = _child("c1-1-1", "c1-1", "c1-1", 1)
    g2 = _child("c1-1-2", "c1-1", "c1-1", 2)
    rows = [p] + kids + [g1, g2]
    eff = effective_boosted_ids({"t-parent"}, rows)
    assert {"c1-1-1", "c1-1-2"} <= eff
    assert eff == {"t-parent", "c1-1", "c1-2", "c1-3", "c1-1-1", "c1-1-2"}


def test_a_root_known_only_by_campaign_id_still_reaches_its_children():
    # The producer row has no task_id, so its node id is its campaign_id (c1), exactly as
    # relay.task_tree._node_id resolves it. Children carry campaign_id c1.
    p = _parent(None, "c1")
    kids = [_child("c1-%d" % i, "c1", "c1", i) for i in (1, 2, 3)]
    rows = [p] + kids
    eff = effective_boosted_ids({"c1"}, rows)
    assert eff == {"c1", "c1-1", "c1-2", "c1-3"}


def test_a_boosted_parent_reaches_children_even_when_its_own_row_is_gone():
    # The parent row was archived out of status.json; only the children remain, still
    # carrying campaign_id c1. Boosting the recorded parent id (c1) still lifts them via
    # the campaign linkage.
    kids = [_child("c1-%d" % i, "c1", "c1", i) for i in (1, 2, 3)]
    eff = effective_boosted_ids({"c1"}, kids)
    assert {"c1-1", "c1-2", "c1-3"} <= eff


def test_a_boosted_id_with_no_matching_row_is_kept():
    eff = effective_boosted_ids({"ghost"}, [])
    assert eff == {"ghost"}


def test_boosted_ids_from_rows_reads_the_default_flag():
    p, kids = _family()
    p[DEFAULT_FLAG_FIELD] = True
    rows = [p] + kids
    assert boosted_ids_from_rows(rows) == {"t-parent"}


def test_boosted_ids_from_rows_reads_a_custom_flag_field():
    p, kids = _family()
    kids[0]["pinned"] = True
    rows = [p] + kids
    assert boosted_ids_from_rows(rows, flag_field="pinned") == {"c1-1"}
    assert boosted_ids_from_rows(rows) == set()  # default flag is absent


def test_propagate_boost_reads_the_flag_and_flows_it_down():
    p, kids = _family()
    p[DEFAULT_FLAG_FIELD] = True
    rows = [p] + kids
    eff = propagate_boost(rows)
    assert eff == {"t-parent", "c1-1", "c1-2", "c1-3"}


def test_propagate_boost_merges_extra_ledger_ids():
    # A ledger recorded a boost on an id whose row is no longer present.
    p, kids = _family()
    rows = [p] + kids
    eff = propagate_boost(rows, extra_boosted_ids=["t-parent"])
    assert eff == {"t-parent", "c1-1", "c1-2", "c1-3"}


def test_a_parent_cycle_cannot_hang_the_walk():
    # a -> b -> a is a corrupt parent chain; the function must terminate and not raise.
    a = {"name": "a", "task_id": "a", "parent_task_id": "b", "campaign_id": "", "role": "subtask"}
    b = {"name": "b", "task_id": "b", "parent_task_id": "a", "campaign_id": "", "role": "subtask"}
    eff = effective_boosted_ids({"a"}, [a, b])
    assert "a" in eff and "b" in eff


def test_malformed_rows_are_skipped_not_raised_on():
    p, kids = _family()
    rows = [p, None, "not a dict", 42, kids[0]]
    # must not raise; the one real child still links under the parent
    eff = effective_boosted_ids({"t-parent"}, rows)
    assert "t-parent" in eff and "c1-1" in eff


def test_is_boosted_helper_coerces_and_checks():
    eff = {"t-parent", "c1-1"}
    assert is_boosted("t-parent", eff) is True
    assert is_boosted("  c1-1  ", eff) is True  # trimmed
    assert is_boosted("c1-9", eff) is False
    assert is_boosted("", eff) is False


def test_two_independent_families_do_not_cross_contaminate():
    pa, ka = _parent("t-a", "ca"), [_child("ca-1", "t-a", "ca", 1), _child("ca-2", "t-a", "ca", 2)]
    pb, kb = _parent("t-b", "cb"), [_child("cb-1", "t-b", "cb", 1), _child("cb-2", "t-b", "cb", 2)]
    rows = [pa] + ka + [pb] + kb
    eff = effective_boosted_ids({"t-a"}, rows)
    assert eff == {"t-a", "ca-1", "ca-2"}
    assert not (eff & {"t-b", "cb-1", "cb-2"})
