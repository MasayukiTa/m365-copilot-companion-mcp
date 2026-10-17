# -*- coding: utf-8 -*-
"""Jobs pending while nothing consumes the queue must be noticed, healed and announced.

2026-10-17: fleet_submit jobs sat in .fleet/tasks/pending for 4 h 45 min and 12 min because
scripts/supervisor.ps1 (the only caller of the task router's drain) was gone while the MCP
server kept answering. The reply said "queued"; the cockpit said "unassigned"; nothing
restarted anything. These tests pin the three things that were missing:

  (a) pending job older than the limit + no consumer -> ONE restart through the product's own
      start_all, rate limited, with a log line saying why;
  (b) the same condition, while it persists, writes an alarm naming the job's age and whether
      it is being restarted or could not be;
  (c) fleet_submit's reply says whether a consumer is alive at submit time.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import consumer_watch as CW  # noqa: E402
from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1_800_000_000.0


def _src(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def root(tmp_path):
    os.makedirs(tmp_path / ".fleet" / "tasks" / "pending")
    return str(tmp_path)


def _job(root, name, age_s):
    p = os.path.join(root, ".fleet", "tasks", "pending", name + ".json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.utime(p, (NOW - age_s, NOW - age_s))


class Launcher:
    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def __call__(self, root):
        self.calls += 1
        return self.ok


def _alarm(root):
    with open(os.path.join(root, ".fleet", "consumer_alarm.json"), encoding="utf-8") as fh:
        return json.load(fh)


# -- (a) restart, once, rate limited, with the reason logged ----------------------------------

def test_old_pending_job_and_no_consumer_restarts_once(root):
    _job(root, "a", 600)
    ln = Launcher()
    assert CW.tick(root, now=NOW, alive=False, launcher=ln) == "restarting"
    assert ln.calls == 1
    # 15 s, 60 s, 200 s later it is still dead -- the restart is in progress, not repeated.
    for dt in (15, 60, 200):
        CW.tick(root, now=NOW + dt, alive=False, launcher=ln)
    assert ln.calls == 1
    log = open(os.path.join(root, ".fleet", "consumer_watch.log"), encoding="utf-8").read()
    assert "restarting through start_all.ps1" in log
    assert "oldest pending job is 600s old" in log          # WHY is in the line


def test_restarts_are_bounded_per_hour(root):
    _job(root, "a", 600)
    ln = Launcher()
    states = [CW.tick(root, now=NOW + i * 310, alive=False, launcher=ln) for i in range(10)]
    assert ln.calls == CW.RESTART_MAX_PER_HOUR
    assert states[0] == "restarting"
    assert states[-1] == "failed"                             # gave up inside the hour


def test_no_restart_when_a_consumer_is_alive_or_cannot_be_asked(root):
    _job(root, "a", 6000)
    ln = Launcher()
    assert CW.tick(root, now=NOW, alive=True, launcher=ln) == "ok"
    assert CW.tick(root, now=NOW, alive=None, launcher=ln) == "unknown"
    assert ln.calls == 0


def test_no_restart_for_a_young_job_or_an_empty_queue(root):
    ln = Launcher()
    assert CW.tick(root, now=NOW, alive=False, launcher=ln) == "ok"        # nothing pending
    _job(root, "a", CW.STALE_AFTER_S - 5)
    assert CW.tick(root, now=NOW, alive=False, launcher=ln) == "waiting"   # one drain behind
    assert ln.calls == 0


# -- (b) the alarm on the surface the person sees ---------------------------------------------

def test_alarm_names_the_age_and_says_it_is_being_restarted(root):
    _job(root, "a", 4 * 3600 + 45 * 60)
    CW.tick(root, now=NOW, alive=False, launcher=Launcher())
    a = _alarm(root)
    assert a["state"] == "restarting"
    assert "4時間45分" in a["text_ja"] and "自動で再起動" in a["text_ja"]
    assert "4 h 45 min" in a["text_en"] and "restarted automatically" in a["text_en"]


def test_alarm_says_to_rerun_start_all_when_the_restart_did_not_work(root):
    _job(root, "a", 900)
    ln = Launcher()
    for i in range(6):
        CW.tick(root, now=NOW + i * 310, alive=False, launcher=ln)
    a = _alarm(root)
    assert a["state"] == "failed"
    assert "start_all.bat" in a["text_ja"] and "start_all.bat" in a["text_en"]


def test_alarm_is_withdrawn_when_a_consumer_is_back(root):
    _job(root, "a", 900)
    CW.tick(root, now=NOW, alive=False, launcher=Launcher())
    assert os.path.exists(os.path.join(root, ".fleet", "consumer_alarm.json"))
    CW.tick(root, now=NOW + 30, alive=True, launcher=Launcher())
    assert not os.path.exists(os.path.join(root, ".fleet", "consumer_alarm.json"))


# -- (c) the submit reply -----------------------------------------------------------------------

def test_fleet_submit_says_a_consumer_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    monkeypatch.setattr(CW, "consumer_alive", lambda: False)
    out = FI.fleet_submit("check the quarterly numbers in the finance workbook")
    assert out.startswith("queued ")
    assert "no queue consumer" in out and "start_all.bat" in out


def test_fleet_submit_says_a_consumer_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    monkeypatch.setattr(CW, "consumer_alive", lambda: True)
    out = FI.fleet_submit("summarise the open purchase orders for supplier Z")
    assert "A queue consumer (the supervisor) is running." in out


# -- wiring: the pieces that make it reach a person --------------------------------------------

def test_the_server_starts_the_watcher():
    s = _src("main.py")
    assert "from relay.consumer_watch import start_watcher" in s


def test_the_cockpit_shows_and_publishes_the_alarm():
    s = _src("ui/FleetCockpit.Health.cs")
    assert "consumer_alarm.json" in s and "ReadConsumerAlarm()" in s
    assert '\\"consumer_alarm\\"' in s


def test_the_supervisor_leaves_an_exit_record():
    s = _src("scripts/supervisor.ps1")
    assert "leaving its loop" in s and "} finally {" in s
