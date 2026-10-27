# -*- coding: utf-8 -*-
"""A boosted goal takes the NEXT free admission slot -- scheduler side, EXECUTED.

## The requirement (owner, 2026-10-27)

「タスクいっぱい並んでる中で優先順位を手動で最高位まで上げるみたいなのができるといいね」

A job that is already waiting must be liftable to the front of the admission queue AFTER the
fact. fleet_submit(priority=True) only helps at submission; this is the already-queued half.

## What this exercises

`relay.relay_fleet.reorder_pending_by_boost(pending, boosted_jids)` -- the pure scheduler
helper the sweep calls once per tick, just before the `while pending:` admission loop in
`run_relay_fleet`. Admission walks `pending` from the front and defers (never kills) when a
slot or the disk is short, so a boosted goal lifted to the front is exactly what takes the
next free slot ahead of the others, and what pushes a non-boosted goal's next turn back
without stopping it.

The boost STATE (who/when, the .fleet/tasks/boosts/<jid>.json markers and the audit) belongs
to relay/priority_boost.py and is tested separately; this file only proves the ordering the
scheduler derives from a given boosted set.

## What is NOT executed here

The live browser sweep, socket admission, and disk gate: those need a real fleet. The ordering
they consume is this function's output, and that is what is pinned here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.relay_fleet import reorder_pending_by_boost


class _W:
    """A stand-in for a RelayWorker: only `jid` and `name` are read by the helper."""
    def __init__(self, name, jid=None):
        self.name = name
        self.jid = jid

    def __repr__(self):
        return "W(%s,%s)" % (self.name, self.jid)


def test_a_boosted_goal_moves_to_the_front():
    a, b, c = _W("w0", "ja"), _W("w1", "jb"), _W("w2", "jc")
    pending = [a, b, c]
    out = reorder_pending_by_boost(pending, {"jc"})
    assert out[0] is c, out
    # the two non-boosted keep their order behind it
    assert out[1:] == [a, b], out


def test_the_boost_is_stable_within_each_group():
    # Two boosted and three not, interleaved. Each group keeps its own relative order; the
    # boosted ones are simply lifted past the rest -- equals are never reshuffled.
    w = [_W("w0", "j0"), _W("w1", "j1"), _W("w2", "j2"), _W("w3", "j3"), _W("w4", "j4")]
    out = reorder_pending_by_boost(w, {"j1", "j3"})
    assert [x.name for x in out] == ["w1", "w3", "w0", "w2", "w4"], out


def test_a_worker_without_a_jid_is_never_boosted():
    # A bare -g goal / interactive retry has jid=None and can carry no boost marker, so it
    # stays where it was even if its (absent) id were somehow in the set.
    a, b = _W("w0", None), _W("w1", "jb")
    out = reorder_pending_by_boost([a, b], {"jb"})
    assert out == [b, a], out
    out2 = reorder_pending_by_boost([a, b], {""})
    assert out2 == [a, b], out2   # empty-string id does not match a None-jid worker's ""


def test_nothing_boosted_leaves_the_order_untouched():
    w = [_W("w0", "j0"), _W("w1", "j1"), _W("w2", "j2")]
    assert reorder_pending_by_boost(w, set()) == w
    assert reorder_pending_by_boost(w, {"nope"}) == w


def test_everything_boosted_leaves_the_order_untouched():
    w = [_W("w0", "j0"), _W("w1", "j1")]
    assert reorder_pending_by_boost(w, {"j0", "j1"}) == w


def test_no_goal_is_dropped_by_the_reorder():
    # A boost DEFERS a non-boosted goal; it must never remove one. The output is a permutation
    # of the input -- same members, same count.
    w = [_W("w%d" % i, "j%d" % i) for i in range(6)]
    out = reorder_pending_by_boost(w, {"j4", "j0"})
    assert len(out) == len(w)
    assert set(id(x) for x in out) == set(id(x) for x in w)


def test_an_empty_queue_is_returned_as_a_new_list():
    out = reorder_pending_by_boost([], {"j0"})
    assert out == []
    src = [_W("w0", "j0")]
    out = reorder_pending_by_boost(src, set())
    assert out == src and out is not src   # a NEW list, so the caller's rewrite is explicit
