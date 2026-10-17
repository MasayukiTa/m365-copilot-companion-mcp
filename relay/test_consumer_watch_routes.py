# -*- coding: utf-8 -*-
"""ALL work submitted through ANY route must move to real work, or be announced.

The owner's criterion (2026-10-18): not "do the jobs I submitted run" but "does all work submitted
by anyone, through any route, actually move". relay/test_consumer_watch.py pins the supervisor and
tasks/pending; this pins every other route the work can take, with the consumers stubbed:

  for_fleet / awaiting_ack   the router's parked goals (consumer: the supervisor's router pass)
  commands                   commands.d add_goal files from the cockpit, chat, add_goal_to_live_fleet
                             and code_task (consumer: ONLY a live fleet_runner)
  running                    jobs the router claimed and never finished (consumer: the router)

For each: the condition "older than N and nothing can consume it", the repair where one is safe,
and an alarm that names the route and the age and says what the owner does where it is not.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import consumer_watch as CW  # noqa: E402
from relay import task_router as TR  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture
def root(tmp_path):
    for sub in (("tasks", "pending"), ("tasks", "for_fleet"), ("tasks", "awaiting_ack"),
                ("tasks", "running"), ("commands.d",)):
        os.makedirs(tmp_path.joinpath(*sub))
    return str(tmp_path)


def _put(root, route, name, age_s, body="{}"):
    sub, ext, _who = CW.ROUTES[route]
    p = os.path.join(root, *sub, name + ext)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


def _goal_cmd(root, name, age_s):
    return _put(root, "commands", name, age_s, json.dumps({"add_goal": [{"text": "do the thing"}]}))


class Spy:
    def __init__(self, result=(True, "pid 1 running")):
        self.calls = []
        self.result = result

    def __call__(self, *a):
        self.calls.append(a)
        return self.result


def _alarm(root):
    with open(os.path.join(root, "consumer_alarm.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _tick(root, dt=0, alive=True, live=False, router=True, autostart=(True, "ok"),
          launcher=None, adopter=None):
    return CW.tick(root, now=NOW + dt, alive=alive, live=live, router=router, autostart=autostart,
                   launcher=launcher or Spy(), adopter=adopter or Spy())


# -- commands.d: a command file no live run will ever read ---------------------------------------

def test_a_command_with_no_live_run_starts_a_run_that_adopts_it(root):
    p = _goal_cmd(root, "0001", CW.COMMANDS_NO_RUN_AFTER_S + 60)
    ad = Spy()
    assert _tick(root, adopter=ad) == "restarting"
    assert ad.calls == [(root, p)]                      # exactly that file, through the adopt entry
    a = _alarm(root)
    assert a["routes"][0]["route"] == "commands" and a["routes"][0]["repair"] == "adopt"
    assert "commands.d" in a["text_en"] and "6 min" in a["text_en"]
    assert "commands.d" in a["text_ja"] and "6分" in a["text_ja"]
    # still no run 15 s, 60 s and 200 s later: the start is in progress, not repeated
    for dt in (15, 60, 200):
        _tick(root, dt=dt, adopter=ad)
    assert len(ad.calls) == 1
    log = open(os.path.join(root, "consumer_watch.log"), encoding="utf-8").read()
    assert "adopts the command" in log and "no run is live" in log


def test_adopt_restarts_are_bounded_per_hour(root):
    _goal_cmd(root, "0001", 3000)
    ad = Spy()
    states = [_tick(root, dt=i * 310, adopter=ad) for i in range(10)]
    assert len(ad.calls) == CW.RESTART_MAX_PER_HOUR
    assert states[0] == "restarting" and states[-1] == "failed"


def test_a_young_command_or_a_control_command_is_not_stuck_work(root):
    _goal_cmd(root, "0001", CW.COMMANDS_NO_RUN_AFTER_S - 10)
    _put(root, "commands", "0002", 9000, json.dumps({"steer": {"worker": "w0", "text": "x"}}))
    _put(root, "commands", "0003", 9000, "{ torn")
    ad = Spy()
    assert _tick(root, adopter=ad) == "ok"
    assert ad.calls == []
    assert not os.path.exists(os.path.join(root, "consumer_alarm.json"))


def test_a_command_with_no_run_and_autostart_blocked_says_why_and_does_not_start(root):
    _goal_cmd(root, "0001", 900)
    ad = Spy()
    st = _tick(root, autostart=(False, "autostart is off (FLEET_INTAKE_AUTOSTART)"), adopter=ad)
    assert st == "failed" and ad.calls == []
    a = _alarm(root)
    assert "FLEET_INTAKE_AUTOSTART" in a["text_en"] and "start_all.bat" in a["text_en"]
    assert "15 min" in a["text_en"] and "15分" in a["text_ja"]


def test_an_adopt_that_did_not_take_effect_is_not_called_a_start(root):
    _goal_cmd(root, "0001", 900)
    st = _tick(root, adopter=Spy((False, "the adopting run (pid 1) had already exited with code 3")))
    assert st == "failed"
    a = _alarm(root)
    assert "could not be started automatically" in a["text_en"]
    assert "A run that adopts it is being started" not in a["text_en"]
    log = open(os.path.join(root, "consumer_watch.log"), encoding="utf-8").read()
    assert "DID NOT TAKE EFFECT" in log and "exited with code 3" in log


def test_a_live_run_that_does_not_read_its_commands_is_an_alarm_not_a_second_run(root):
    _goal_cmd(root, "0001", CW.COMMANDS_STALLED_AFTER_S + 30)
    ad = Spy()
    assert _tick(root, live=True, adopter=ad) == "failed"
    assert ad.calls == []
    assert "live but has not read it" in _alarm(root)["text_en"]
    # inside the measured drain lag a live run is simply slow
    os.remove(os.path.join(root, "consumer_alarm.json"))
    _goal_cmd(root, "0001", CW.COMMANDS_STALLED_AFTER_S - 60)
    assert _tick(root, live=True, adopter=ad) == "ok"
    assert not os.path.exists(os.path.join(root, "consumer_alarm.json"))


def test_the_alarm_is_withdrawn_when_the_command_is_consumed(root):
    p = _goal_cmd(root, "0001", 900)
    _tick(root)
    assert os.path.exists(os.path.join(root, "consumer_alarm.json"))
    os.remove(p)
    assert _tick(root, dt=30) == "ok"
    assert not os.path.exists(os.path.join(root, "consumer_alarm.json"))


# -- for_fleet / awaiting_ack / pending: the supervisor's router --------------------------------

@pytest.mark.parametrize("route", ["pending", "for_fleet", "awaiting_ack"])
def test_every_supervisor_route_restarts_it_once_and_names_the_route(root, route):
    _put(root, route, "a", 600)
    ln = Spy()
    assert _tick(root, alive=False, launcher=ln) == "restarting"
    assert len(ln.calls) == 1
    a = _alarm(root)
    assert a["routes"][0]["route"] == route
    assert CW.ROUTE_LABEL[route][1] in a["text_en"] and "10 min" in a["text_en"]
    assert CW.ROUTE_LABEL[route][0] in a["text_ja"]
    for dt in (15, 100):
        _tick(root, dt=dt, alive=False, launcher=ln)
    assert len(ln.calls) == 1


@pytest.mark.parametrize("route", ["pending", "for_fleet", "awaiting_ack"])
def test_a_young_supervisor_route_waits(root, route):
    _put(root, route, "a", CW.STALE_AFTER_S - 5)
    ln = Spy()
    assert _tick(root, alive=False, launcher=ln) == "waiting"
    assert ln.calls == []


def test_a_supervisor_that_is_alive_but_not_draining_is_an_alarm_not_a_restart(root):
    # the 15 s drain that failed on every pass while the supervisor stayed up (the "\t" defect)
    _put(root, "pending", "a", CW.STALLED_AFTER_S + 30)
    ln = Spy()
    assert _tick(root, alive=True, launcher=ln) == "failed"
    assert ln.calls == []
    a = _alarm(root)
    assert a["routes"][0]["cause"] == "router_stalled"
    assert "not draining" in a["text_en"] and "start_all.bat" in a["text_en"]
    # a normal pickup is one 15 s drain: well inside the limit, nothing is said
    os.remove(os.path.join(root, "consumer_alarm.json"))
    _put(root, "pending", "a", 40)
    assert _tick(root, alive=True, launcher=ln) == "ok"


def test_a_pending_entry_held_by_a_live_cli_run_is_not_waiting_work(root):
    _put(root, "pending", "cli1", 2000,
         json.dumps({"owner_pid": os.getpid(), "created": NOW - 2000}))
    ln = Spy()
    assert _tick(root, alive=False, launcher=ln) in ("ok", "waiting")
    assert ln.calls == []
    assert not os.path.exists(os.path.join(root, "consumer_alarm.json"))


def test_parked_goals_with_autostart_blocked_wait_out_the_products_backoff_then_alarm(root):
    _put(root, "for_fleet", "g", CW.FOR_FLEET_BLOCKED_AFTER_S - 100)
    reason = "the last autostart launch failed 400s ago; waiting out the 900s backoff"
    assert _tick(root, alive=True, live=False, autostart=(False, reason)) == "ok"
    _put(root, "for_fleet", "g", CW.FOR_FLEET_BLOCKED_AFTER_S + 100)
    assert _tick(root, alive=True, live=False, autostart=(False, reason)) == "failed"
    a = _alarm(root)
    assert a["routes"][0]["cause"] == "blocked" and "waiting out the 900s backoff" in a["text_en"]


def test_parked_goals_that_autostart_could_start_but_did_not_are_a_stalled_router(root):
    _put(root, "for_fleet", "g", CW.STALLED_AFTER_S + 10)
    assert _tick(root, alive=True, live=False, autostart=(True, "ok")) == "failed"
    assert _alarm(root)["routes"][0]["cause"] == "router_stalled"
    # and with a run live the router should have delivered it long ago
    assert _tick(root, alive=True, live=True) == "failed"


# -- running/: claimed by a router that is gone ---------------------------------------------------

def test_a_job_claimed_by_a_router_that_is_gone_is_reported_not_re_run(root):
    p = _put(root, "running", "job1", CW.RUNNING_ORPHAN_AFTER_S + 40)
    ln, ad = Spy(), Spy()
    assert _tick(root, router=False, launcher=ln, adopter=ad) == "failed"
    assert ln.calls == [] and ad.calls == [] and os.path.exists(p)      # nothing moved, nothing run
    a = _alarm(root)
    assert a["routes"][0]["route"] == "running" and a["routes"][0]["repair"] is None
    assert "job1.json" in a["text_en"] and "tasks\\pending" in a["text_en"]
    assert "twice" in a["text_en"]


def test_a_job_the_router_is_still_running_is_not_an_orphan(root):
    _put(root, "running", "job1", CW.RUNNING_ORPHAN_AFTER_S + 40)
    assert _tick(root, router=True) == "ok"
    _put(root, "running", "job1", 30)
    assert _tick(root, router=False) == "ok"


# -- one alarm for all of it ----------------------------------------------------------------------

def test_two_stuck_routes_are_one_alarm_that_names_both(root):
    _put(root, "pending", "a", 700)
    _goal_cmd(root, "0001", 900)
    ln, ad = Spy(), Spy()
    assert _tick(root, alive=False, launcher=ln, adopter=ad) == "restarting"
    assert len(ln.calls) == 1 and len(ad.calls) == 1
    a = _alarm(root)
    assert {r["route"] for r in a["routes"]} == {"pending", "commands"}
    assert "A submitted job" in a["text_en"] and "command channel" in a["text_en"]
    # the cockpit reads these two keys only (ui/FleetCockpit.Health.cs ReadConsumerAlarm)
    assert a["text_ja"] and a["text_en"]


# -- the table itself ---------------------------------------------------------------------------

def test_every_router_queue_directory_has_a_decision():
    watched = {sub[-1] for sub, _e, _w in CW.ROUTES.values() if sub[0] == "tasks"}
    decided = watched | set(CW.UNWATCHED)
    assert set(TR.SUBDIRS) <= decided, (
        "task_router has queue directories nobody here decided about: %s"
        % sorted(set(TR.SUBDIRS) - decided))
    assert not (watched & set(CW.UNWATCHED))
    for name, why in CW.UNWATCHED.items():
        assert why.strip(), name


def test_the_limits_are_derived_from_the_products_own_timings():
    if "TASK_LOCAL_TIMEOUT_S" not in os.environ:
        assert CW.STALLED_AFTER_S == 2 * TR.LOCAL_TIMEOUT_S + 60
        assert CW.RUNNING_ORPHAN_AFTER_S == 3 * TR.LOCAL_TIMEOUT_S
    if not any(k in os.environ for k in ("FLEET_INTAKE_AUTOSTART_BACKOFF_S",
                                         "FLEET_INTAKE_AUTOSTART_GRACE_S")):
        assert CW.FOR_FLEET_BLOCKED_AFTER_S == TR.AUTOSTART_BACKOFF_S + TR.AUTOSTART_GRACE_S
    assert CW.STALE_AFTER_S < CW.STALLED_AFTER_S < CW.COMMANDS_STALLED_AFTER_S


# -- the adopt launch, through a stand-in for the runner ---------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="Windows process creation flags")
def test_the_adopt_launch_passes_the_runner_the_command_and_the_state_dir(tmp_path):
    (tmp_path / "relay").mkdir()
    (tmp_path / "relay" / "__init__.py").write_text("")
    (tmp_path / "relay" / "fleet_runner.py").write_text(
        "import sys, time\n"
        "open(sys.argv[sys.argv.index('--state-dir') + 1] + '/argv.txt', 'w').write('|'.join(sys.argv[1:]))\n"
        "time.sleep(6)\n")
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    cmd = str(fleet / "commands.d" / "x.json")
    took, note = CW.launch_adopt(str(fleet), cmd, repo=str(tmp_path), wait_s=1.0)
    import time as _t
    for _ in range(100):
        if (fleet / "argv.txt").exists():
            break
        _t.sleep(0.1)
    assert (fleet / "argv.txt").read_text().startswith("--adopt-command|" + cmd + "|--state-dir|")
    assert took, note
