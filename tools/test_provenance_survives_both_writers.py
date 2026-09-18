# -*- coding: utf-8 -*-
"""A done-record is built in two places. Both must carry where the job came from.

WHY THERE ARE TWO. relay/task_router builds a done-record in run_job, when a job is claimed and
processed, and again in the late-delivery pass, when a goal that had been parked in for_fleet/
finally reaches a live run. run_job was taught on 2026-09-18 to carry `origin` and `created`
across that boundary, with the reason written beside it -- "a distinction that does not reach
the audit trail is not a distinction". The second writer built its record from scratch.

MEASURED THE SAME DAY. For one job id, two records on disk:

    cli1789703602_14000_0.json            origin={"via": "cli", "source": "...fleet_runner..."}
    cli1789703602_14000_0.delivered.json  origin=null

The second says "nobody knows where this came from" about a job whose origin was sitting in the
first. The provenance of a fleet goal is the only record of whether an instruction came from a
person at the cockpit or from an agent over a tunnel, and the consumer is entitled to treat
those differently -- which it cannot do from a null.

THE SHAPE OF THE MISTAKE, which is why this file exists rather than a one-line patch: a failure
class was fixed at one caller and not swept. The test therefore pins BOTH writers, so the next
one to be added has somewhere to fail.
"""
from __future__ import annotations

import json
import os

import pytest

import relay.task_router as TR


ORIGIN = {"via": "cli", "source": "relay/fleet_runner.py -g something"}


@pytest.fixture()
def state(tmp_path, monkeypatch):
    """A task tree of our own. TR resolves its paths from module state, so point that here
    rather than at the operator's .fleet -- the isolation registry exists for this reason."""
    root = tmp_path / "tasks"
    for sub in ("pending", "awaiting", "awaiting_ack", "done", "for_fleet", "for_claude",
                "running"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(TR, "TASKS", str(root), raising=False)
    return root


def test_run_job_carries_origin_and_created():
    """The first writer, pinned again here so both live in one file."""
    job = {"id": "j1", "type": "fleet_goal", "created": 1_700_000_000.0,
           "payload": {"goal": "x"}, "origin": dict(ORIGIN)}
    rec = TR.run_job(dict(job), now_ts=1_700_000_500.0)
    assert rec["origin"] == ORIGIN
    assert rec["created"] == 1_700_000_000.0


def test_the_late_delivery_record_carries_origin_from_the_earlier_one(state, monkeypatch):
    """THE DEFECT. The pending file is deleted the moment delivery succeeds, so the earlier
    done-record is the only place left holding it."""
    jid = "j2"
    # what the earlier pass wrote, while the goal was still waiting for a run
    (state / "done" / ("%s.json" % jid)).write_text(json.dumps({
        "id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": None,
        "status": "awaiting_fleet", "origin": dict(ORIGIN), "created": 1_700_000_000.0,
    }, ensure_ascii=False), encoding="utf-8")
    # the goal parked on the waiter channel
    (state / "for_fleet" / ("%s.txt" % jid)).write_text("do the thing", encoding="utf-8")

    monkeypatch.setattr(TR, "fleet_is_live", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(TR, "fleet_handoff",
                        lambda goal, j, sd=None: ("dispatched", {"delivered": "add_goal"}),
                        raising=False)

    out = TR._deliver_waiting_goals(now_ts=1_700_000_900.0, state_dir=str(state.parent))

    path = state / "done" / ("%s.delivered.json" % jid)
    assert path.is_file(), "no late-delivery record was written: %r" % (out,)
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["origin"] == ORIGIN, \
        "the late-delivery record lost the provenance the earlier one held: %r" % rec.get("origin")
    assert rec["created"] == 1_700_000_000.0


def test_a_job_with_no_earlier_record_does_not_gain_a_false_origin(state, monkeypatch):
    """An absent origin is a fact. Inventing one would make an unattributable job look
    attributed, which is worse than the null it replaces."""
    jid = "j3"
    (state / "for_fleet" / ("%s.txt" % jid)).write_text("do the thing", encoding="utf-8")
    monkeypatch.setattr(TR, "fleet_is_live", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(TR, "fleet_handoff",
                        lambda goal, j, sd=None: ("dispatched", {"delivered": "add_goal"}),
                        raising=False)

    TR._deliver_waiting_goals(now_ts=1_700_000_900.0, state_dir=str(state.parent))

    path = state / "done" / ("%s.delivered.json" % jid)
    if path.is_file():
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert "origin" not in rec or rec["origin"] is None
