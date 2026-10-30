# -*- coding: utf-8 -*-
"""On a COLD START the operator's priority goal is offered before the plain batch.

The bug this pins: `_deliver_waiting_goals` offers exactly one goal per pass when no
fleet is live, and it used to pick whichever filename sorted first. A jid is time-ordered,
so a priority goal dropped into a batch waited behind every goal queued before it -- the
very delay the owner complains about (a submit that is received, then sits for 1-2 minutes
before a worker is assigned). The fix reorders the cold-start pass so a parked priority
goal goes first; a LIVE fleet is deliberately left on plain name order because it takes the
whole backlog in one pass and the order does not gate anything there.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def router(tmp_path, monkeypatch):
    import relay.task_router as T

    monkeypatch.setattr(T, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(T, "FLEET_STATE_DIR", str(tmp_path))
    T.ensure_dirs()
    return T


def _park(router, jid, goal, priority=False):
    """Write a for_fleet/<jid>.txt exactly as the router's own writer would."""
    assert router._write_for_fleet(jid, goal, priority=priority)


def test_cold_start_offers_the_priority_goal_before_an_earlier_plain_one(router, tmp_path, monkeypatch):
    # Two plain goals queued first (earlier jids), then the owner's priority goal with a
    # LATER jid -- so plain name order would put it last.
    _park(router, "j01", "plain A")
    _park(router, "j02", "plain B")
    _park(router, "j09", "owner priority", priority=True)

    offered = []
    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: False)
    monkeypatch.setattr(router, "AUTOSTART", True)
    monkeypatch.setattr(router, "autostart_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        router, "autostart_fleet",
        lambda goals, sd=None: offered.extend(goals) or {"ok": True, "pid": 1},
    )

    router._deliver_waiting_goals(state_dir=str(tmp_path))

    # One cold handoff per pass, and it is the priority goal -- not the earlier plain jid.
    assert offered, "a cold start should offer exactly one goal"
    assert offered[0]["text"] == "owner priority"
    assert offered[0]["priority"] is True


def test_cold_start_order_is_stable_within_each_class(router):
    # Same-priority goals keep FIFO-by-jid; only the priority/plain split moves.
    _park(router, "a1", "plain 1")
    _park(router, "b2", "priority 1", priority=True)
    _park(router, "c3", "plain 2")
    _park(router, "d4", "priority 2", priority=True)

    ordered = router._cold_start_order(["a1.txt", "b2.txt", "c3.txt", "d4.txt"])
    assert ordered == ["b2.txt", "d4.txt", "a1.txt", "c3.txt"]


def test_a_live_fleet_keeps_plain_name_order(router, tmp_path, monkeypatch):
    # A live fleet must NOT be reordered: it drains the whole backlog in one pass.
    _park(router, "j01", "plain A")
    _park(router, "j09", "owner priority", priority=True)

    monkeypatch.setattr(router, "fleet_is_live", lambda *a, **k: True)
    router._deliver_waiting_goals(state_dir=str(tmp_path))

    from relay import fleet_runner as FR
    cmds = FR.read_commands(str(tmp_path))
    items = [g for c in cmds for g in c.get("add_goal", [])]
    texts = [g["text"] for g in items]
    # Both delivered; the earlier plain jid is still handed over first (order unchanged).
    assert texts == ["plain A", "owner priority"]


def test_cold_start_order_never_raises_on_a_bad_file(router, tmp_path):
    # An unreadable / malformed name counts as non-priority and keeps its place.
    _park(router, "z1", "plain")
    ordered = router._cold_start_order(["z1.txt", "missing.txt", "not_a_txt"])
    assert ordered[0] == "z1.txt" or ordered[0] == "missing.txt"
    assert set(ordered) == {"z1.txt", "missing.txt", "not_a_txt"}
