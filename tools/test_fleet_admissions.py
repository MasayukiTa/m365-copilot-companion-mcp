# -*- coding: utf-8 -*-
"""Every submission must be visible from ONE CLI command -- including the ones the fleet
never received.

WHY THIS EXISTS. 2026-10-30 the owner typed an instruction into the Fleet Cockpit. The screen
wrote a 'queued / unconfirmed' row into .fleet/health_strip.json with an EMPTY jid, but the
fleet intake (tasks/) never saw it: nothing in pending/, for_fleet/ or awaiting_ack/, and no
worker in status.json. `fleet_queue()` only counts task directories, and `status` only shows
workers, so this submission -- stuck at the screen, never admitted -- was invisible to the CLI
for >12 minutes. The owner's order: "every unsubmitted job, and every submitted-but-unstarted
job, must be visible from the CLI."

These tests pin `fleet_admissions()` (the read-only aggregator) and the `admissions` CLI
subcommand. They are written to FAIL until that aggregator and subcommand exist.

Hermetic: a whole .fleet tree is built under tmp_path and TR.TASKS / TR.FLEET_STATE_DIR are
pointed at it; no real queue is read or written. No Windows-only constructs (pure pathlib/json).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from relay import task_router as TR
from tools import fleet_intake as FI


# --- hermetic .fleet tree -----------------------------------------------------------------

SUBDIRS = ("pending", "running", "done", "awaiting", "awaiting_ack", "for_fleet", "for_claude")


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A private .fleet tree. Yields the .fleet dir; TR is redirected at it."""
    fleet_dir = tmp_path / ".fleet"
    tasks = fleet_dir / "tasks"
    for sub in SUBDIRS:
        (tasks / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(TR, "TASKS", str(tasks), raising=False)
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(fleet_dir), raising=False)
    return fleet_dir


def _write_pending(fleet_dir, jid, goal, created=None, status=None, sub="pending"):
    rec = {"id": jid, "type": "fleet_goal",
           "payload": {"goal": goal}, "created": created or time.time()}
    if status:
        rec["status"] = status
    p = fleet_dir / "tasks" / sub / ("%s.json" % jid)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return p


def _write_for_fleet(fleet_dir, jid, goal):
    p = fleet_dir / "tasks" / "for_fleet" / ("%s.txt" % jid)
    p.write_text(goal, encoding="utf-8")
    return p


def _write_awaiting_ack(fleet_dir, jid, goal, created=None):
    rec = {"id": jid, "goal": goal, "created": created or time.time()}
    p = fleet_dir / "tasks" / "awaiting_ack" / ("%s.json" % jid)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return p


def _write_status(fleet_dir, workers):
    (fleet_dir / "status.json").write_text(
        json.dumps({"workers": workers}, ensure_ascii=False), encoding="utf-8")


def _write_health_strip(fleet_dir, queued):
    (fleet_dir / "health_strip.json").write_text(
        json.dumps({"ts": time.time(), "queued": queued}, ensure_ascii=False), encoding="utf-8")


# --- the aggregator -----------------------------------------------------------------------

def test_aggregator_exists(fleet):
    assert hasattr(FI, "fleet_admissions"), "fleet_admissions() must exist"
    rows = FI.fleet_admissions()
    assert isinstance(rows, list)


def test_screen_waiting_submission_with_empty_jid_is_listed(fleet):
    """THE INCIDENT: a submission that reached only the screen (health_strip.queued, id='')
    and never the fleet intake must still appear, flagged as waiting at the screen."""
    _write_health_strip(fleet, [{
        "id": "", "where": "local", "unconfirmed": True, "age_s": 721.5,
        "label": "投入済み・未着手 12分 ・ フリート未確認",
        "goal_head": "サイズ変更でよろしく。30%くらいに。",
    }])
    rows = FI.fleet_admissions()
    waiting = [r for r in rows if r.get("state") == "awaiting_intake"]
    assert len(waiting) == 1, "the id-less screen submission must be surfaced"
    r = waiting[0]
    assert "サイズ変更" in (r.get("goal_head") or "")
    assert r.get("jid") in ("", None), "it has no jid yet"
    assert r.get("age_s") is not None


def test_pending_job_is_accepted(fleet):
    _write_pending(fleet, "aaaa1111", "queued goal")
    rows = FI.fleet_admissions()
    m = [r for r in rows if r.get("jid") == "aaaa1111"]
    assert len(m) == 1 and m[0]["state"] == "accepted"
    assert m[0]["goal_head"].startswith("queued goal")


def test_for_fleet_job_is_awaiting_fleet(fleet):
    _write_for_fleet(fleet, "bbbb2222", "parked goal")
    rows = FI.fleet_admissions()
    m = [r for r in rows if r.get("jid") == "bbbb2222"]
    assert len(m) == 1 and m[0]["state"] == "awaiting_fleet"


def test_awaiting_ack_job_is_dispatched(fleet):
    _write_awaiting_ack(fleet, "cccc3333", "handed off goal")
    rows = FI.fleet_admissions()
    m = [r for r in rows if r.get("jid") == "cccc3333"]
    assert len(m) == 1 and m[0]["state"] == "dispatched"


def test_live_worker_is_running(fleet):
    _write_status(fleet, [{"jid": "dddd4444", "goal": "worker goal",
                           "status": "working", "closed": False}])
    rows = FI.fleet_admissions()
    m = [r for r in rows if r.get("jid") == "dddd4444"]
    assert len(m) == 1 and m[0]["state"] == "running"


def test_closed_and_terminal_workers_are_not_running(fleet):
    _write_status(fleet, [
        {"jid": "e1", "goal": "g", "status": "done", "closed": True},
        {"jid": "e2", "goal": "g", "status": "done", "closed": False},
    ])
    rows = FI.fleet_admissions()
    running = [r for r in rows if r.get("state") == "running"]
    assert running == [], "a closed or terminal worker is not an unstarted/running admission"


def test_every_source_in_one_list(fleet):
    """All routes in one call: screen-waiting, accepted, awaiting_fleet, dispatched, running."""
    _write_health_strip(fleet, [{"id": "", "unconfirmed": True, "age_s": 10,
                                 "goal_head": "screen one"}])
    _write_pending(fleet, "p1", "pending one")
    _write_for_fleet(fleet, "f1", "forfleet one")
    _write_awaiting_ack(fleet, "a1", "ack one")
    _write_status(fleet, [{"jid": "w1", "goal": "worker one",
                           "status": "working", "closed": False}])
    rows = FI.fleet_admissions()
    states = sorted(r["state"] for r in rows)
    assert states == sorted(["awaiting_intake", "accepted", "awaiting_fleet",
                             "dispatched", "running"])
    for r in rows:
        assert "submitted_ts" in r and "age_s" in r and "goal_head" in r and "jid" in r


def test_each_row_has_required_fields(fleet):
    _write_pending(fleet, "p9", "a goal here", created=time.time() - 30)
    rows = FI.fleet_admissions()
    r = [x for x in rows if x.get("jid") == "p9"][0]
    for key in ("state", "submitted_ts", "age_s", "goal_head", "jid", "where"):
        assert key in r, "row missing %r" % key
    assert r["age_s"] >= 29


# --- the CLI ------------------------------------------------------------------------------

def test_cli_admissions_lists_every_source(fleet, capsys):
    _write_health_strip(fleet, [{"id": "", "unconfirmed": True, "age_s": 10,
                                 "goal_head": "screen waiting goal"}])
    _write_pending(fleet, "p1", "pending goal")
    _write_status(fleet, [{"jid": "w1", "goal": "running goal",
                           "status": "working", "closed": False}])
    rc = FI._cli(["admissions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "screen waiting goal" in out
    assert "pending goal" in out
    assert "running goal" in out
    assert "awaiting_intake" in out


def test_cli_admissions_empty_is_clean(fleet, capsys):
    rc = FI._cli(["admissions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() != ""


# --- fanout children and de-duplication ---------------------------------------------------

def test_fanout_children_sharing_one_jid_are_distinct_rows(fleet):
    """A fanout parent's children share ONE jid but have distinct task_ids. Each must show as
    its own row -- keying on jid alone would collapse six children into one."""
    _write_status(fleet, [
        {"jid": "par1", "task_id": "par1-1", "goal": "child one",
         "status": "waiting", "closed": False},
        {"jid": "par1", "task_id": "par1-2", "goal": "child two",
         "status": "waiting", "closed": False},
        {"jid": "par1", "task_id": "par1-3", "goal": "child three",
         "status": "waiting", "closed": False},
    ])
    rows = FI.fleet_admissions()
    running = [r for r in rows if r.get("state") == "running"]
    assert len(running) == 3, "each fanout child is its own row"
    assert {r["jid"] for r in running} == {"par1-1", "par1-2", "par1-3"}
    assert all(r.get("parent_jid") == "par1" for r in running)


def test_worker_age_comes_from_phase_events(fleet):
    """A worker row's age is derived from the earliest phase_events ts, so `running` jobs show
    a real age rather than '?'."""
    started = time.time() - 45
    _write_status(fleet, [{
        "jid": "w7", "task_id": "w7-1", "goal": "timed goal",
        "status": "waiting", "closed": False,
        "phase_events": [{"ts": started, "event": "pending"},
                         {"ts": started + 1, "event": "ready"}],
    }])
    rows = FI.fleet_admissions()
    r = [x for x in rows if x.get("jid") == "w7-1"][0]
    assert r["submitted_ts"] is not None
    assert r["age_s"] >= 44


def test_same_jid_from_two_sources_is_listed_once_per_state(fleet):
    """If a pending file lingers while the same job is also on a worker, the list stays honest:
    one accepted row and one running row, not duplicate accepted rows."""
    _write_pending(fleet, "dupjid", "same goal")
    _write_pending(fleet, "dupjid", "same goal", sub="running")  # overwrites nothing; other dir
    rows = FI.fleet_admissions()
    accepted = [r for r in rows if r.get("state") == "accepted" and r.get("jid") == "dupjid"]
    assert len(accepted) == 1, "one accepted row per jid even when it is in two task dirs"
