# -*- coding: utf-8 -*-
"""An interrupted run comes back by itself, and ahead of the queued goals.

THE INCIDENT, twice. A coordinator died; the reaper recorded the run as interrupted
(.fleet/interrupted/<run>.json, state=pending) and nothing resumed it:

  (a) the supervisor's per-cycle resume was a DRY RUN unless a command-line flag was passed -- a
      switch the operator cannot reach from the cockpit, so for them the feature did not exist;
  (b) a goal was waiting in the queue, the router saw "no fleet is live", and started a FRESH
      coordinator that ignored the snapshot. The interrupted trees were lost.

What these tests pin: the setting `fleet_auto_resume` (default on) is the switch; with it on a
pending snapshot holds the queue until the resume has happened; with it off nothing changes; the
loop guard's final refusals never hold the queue (so a goal cannot be stranded); and the screen is
told what the gate decided. The supervisor half (the PowerShell) is in
scripts/test_supervisor_fleet_resume_guard.py.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_resume as FRES   # noqa: E402
from relay import task_router as TR       # noqa: E402
from tools import settings_keys as SK     # noqa: E402
from tools import settings_path as SP     # noqa: E402

NOW = 2_000_000.0
GB = 1024 ** 3


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A private settings.txt: the checkout's real one and %APPDATA%'s must never be read."""
    path = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    monkeypatch.delenv(FRES.AUTO_RESUME_ENV, raising=False)

    def put(value):
        if value is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text("fleet_auto_resume=%s\n" % value, encoding="utf-8")
    return put


@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "fleet"
    (sd / "interrupted").mkdir(parents=True)
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(sd))
    monkeypatch.setattr(TR, "AUTOSTART", True)
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://example.invalid/agent")
    TR.ensure_dirs()
    return sd


def _snap(sd, run_id="r1", state="pending", written=None, **extra):
    rec = {"schema": 1, "run_id": run_id, "state": state,
           "written_ts": time.time() if written is None else written,
           "marker": {"pid": 1, "argv": ["--x"], "resume_argv": ["--x"]},
           "interrupted": {"free_bytes_at_detection": 1000}, "resume": {"count": 0}}
    rec.update(extra)
    p = os.path.join(str(sd), "interrupted", run_id + ".json")
    with io.open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return p


# -- the setting -------------------------------------------------------------------------------

def test_the_registry_says_on_and_the_reader_agrees(settings):
    assert SK.default("fleet_auto_resume") == FRES.AUTO_RESUME_DEFAULT == "on"
    assert SK.effect("fleet_auto_resume") == SK.EACH_GATE
    settings(None)
    assert FRES.auto_resume_setting() == "on"
    settings("off")
    assert FRES.auto_resume_setting() == "off"
    settings("ON")
    assert FRES.auto_resume_setting() == "on"
    settings("maybe")                                   # junk is the default, never an accident
    assert FRES.auto_resume_setting() == "on"


def test_the_env_override_beats_the_setting_in_both_directions(settings, monkeypatch):
    settings("on")
    monkeypatch.setenv(FRES.AUTO_RESUME_ENV, "0")
    assert FRES.auto_resume_enabled() is False
    settings("off")
    monkeypatch.setenv(FRES.AUTO_RESUME_ENV, "1")
    assert FRES.auto_resume_enabled() is True
    monkeypatch.setenv(FRES.AUTO_RESUME_ENV, "")        # empty = unset
    assert FRES.auto_resume_enabled() is False


# -- the ordering: the interrupted run first, the queue after -------------------------------------

def test_a_pending_snapshot_holds_the_queue_when_auto_resume_is_on(state, settings):
    settings("on")
    _snap(state)
    may, why = TR.autostart_status(str(state))
    assert may is False
    # ok or below_floor, depending on this machine's free space: both are "the resume is coming"
    assert "interrupted run" in why and "interrupted_run_pending:" in why


def test_with_auto_resume_off_the_queue_is_not_held(state, settings):
    settings("off")
    _snap(state)
    may, why = TR.autostart_status(str(state))
    assert may is True, why


def test_queued_goal_plus_pending_snapshot_starts_no_second_coordinator(state, settings, monkeypatch):
    """THE LIVE-TEST LOSS. A goal waits, the coordinator is dead, a snapshot is pending: the goal
    must be parked for the resumed run, not handed to a fresh coordinator."""
    settings("on")
    _snap(state)
    launched = []
    monkeypatch.setattr(TR, "autostart_fleet", lambda goals, sd=None, **kw: launched.append(goals) or {"ok": True, "pid": 1})
    status, result = TR.fleet_handoff("summarise the folder", "jid1", str(state))
    assert launched == [], "a fresh coordinator was started over a pending interrupted run"
    assert status == "awaiting_fleet"
    assert "interrupted" in result["note"]
    assert os.path.isfile(os.path.join(TR.TASKS, "for_fleet", "jid1.txt")), "the goal was lost, not parked"


def test_once_the_resumed_run_is_live_the_queued_goal_joins_it(state, settings, monkeypatch):
    settings("on")
    _snap(state, state="resumed")                       # the supervisor consumed the snapshot
    with io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)
    launched = []
    monkeypatch.setattr(TR, "autostart_fleet", lambda *a, **k: launched.append(a) or {"ok": True})
    status, result = TR.fleet_handoff("join me", "jid2", str(state))
    assert status == "dispatched" and result["delivered"] == "add_goal"
    assert launched == []


def test_with_auto_resume_off_a_queued_goal_still_autostarts_as_before(state, settings, monkeypatch):
    settings("off")
    _snap(state)
    launched = []
    monkeypatch.setattr(TR, "autostart_fleet", lambda goals, sd=None, **kw: launched.append(goals) or {"ok": True, "pid": 7})
    status, result = TR.fleet_handoff("go", "jid3", str(state))
    assert len(launched) == 1 and status == "dispatched" and result["delivered"] == "autostart"


def test_the_launch_guard_covers_the_window_before_the_resumed_run_has_a_marker(state, settings):
    settings("off")                                     # the guard does not depend on the setting
    FRES.write_launch_guard(str(state), 4321, "r1", NOW)
    assert FRES.autostart_hold(str(state), now=NOW + 5, pid_alive=lambda p: p == 4321) == "resume_launching"
    assert FRES.autostart_hold(str(state), now=NOW + 5, pid_alive=lambda p: False) == ""   # it died
    assert FRES.autostart_hold(str(state), now=NOW + FRES.LAUNCH_GUARD_S + 1,
                               pid_alive=lambda p: True) == ""                              # expired


def test_a_guard_written_before_the_launch_has_no_pid_yet_and_still_holds(state, settings):
    settings("off")
    FRES.write_launch_guard(str(state), 0, "r1", NOW)
    assert FRES.autostart_hold(str(state), now=NOW + 1, pid_alive=lambda p: False) == "resume_launching"
    FRES.clear_launch_guard(str(state))                 # the launch failed
    assert FRES.autostart_hold(str(state), now=NOW + 1, pid_alive=lambda p: False) == ""


@pytest.mark.parametrize("name,extra,free,floor,held", [
    ("fresh", {}, 50 * GB, None, True),
    ("backoff", {"resume": {"count": 1, "last_ts": NOW - 10}}, 50 * GB, None, True),
    ("below_floor", {}, 1 * GB, 6.0, True),
    # final refusals of the automatic path: holding the queue for these would strand it for ever
    ("loop_cap", {"resume": {"count": 3, "last_ts": 1.0}}, 50 * GB, None, False),
    ("stop_requested", {"stop_requested": True}, 50 * GB, None, False),
    ("gave_up", {"state": "gave_up"}, 50 * GB, None, False),
    ("already_resumed", {"state": "resumed"}, 50 * GB, None, False),
    ("ancient", {"written_ts": NOW - FRES.HOLD_MAX_S - 1}, 50 * GB, None, False),
])
def test_only_a_resume_that_is_coming_holds_the_queue(state, settings, name, extra, free, floor, held):
    settings("on")
    _snap(state, written=extra.pop("written_ts", NOW - 5), **extra)
    got = FRES.autostart_hold(str(state), now=NOW, free_bytes=free, floor_gb=floor,
                              pid_alive=lambda p: False)
    assert bool(got) is held, (name, got)


def test_the_hold_is_the_gates_own_verdict_not_a_second_copy(state, settings):
    """Every refusal the gate can give is either held or not by HOLD_REASONS alone, so a new
    reason added to resume_gate defaults to NOT holding the queue (the safe side)."""
    assert set(FRES.HOLD_REASONS) == {"ok", "backoff", "below_floor"}
    ok, reason = FRES.resume_gate({"state": "pending"}, NOW, 50 * GB, None, "")
    assert (ok, reason) == (True, "ok") and reason in FRES.HOLD_REASONS


# -- what the screen is told ----------------------------------------------------------------------

def test_the_gate_decisions_are_recorded_for_the_screen(state, settings):
    settings("on")
    p = _snap(state, run_id="rA")
    assert FRES.record_blocked(p, NOW, "backoff", 1, "")
    rep = FRES.auto_resume_report(str(state))
    assert rep["setting"] == "on" and rep["pending_snapshots"] == 1
    assert rep["last_decision"]["decision"] == "waiting" and rep["last_decision"]["reason"] == "backoff"
    assert rep["last_decision"]["run_id"] == "rA"
    assert FRES.record_resume(p, NOW + 1, 1, "")
    rep = FRES.auto_resume_report(str(state))
    assert rep["last_decision"]["decision"] == "resumed" and rep["pending_snapshots"] == 0
    p2 = _snap(state, run_id="rB", resume={"count": 3})
    FRES.record_blocked(p2, NOW + 2, "max_resumes", 1, "")
    last = FRES.auto_resume_report(str(state))["last_decision"]
    assert (last["decision"], last["reason"]) == ("refused", "max_resumes")


def test_the_report_states_the_setting_the_supervisor_reads(state, settings):
    settings("off")
    assert FRES.auto_resume_report(str(state))["setting"] == "off"
    assert FRES.auto_resume_report(str(state))["last_decision"] is None


def test_a_decision_reaches_an_idle_status_json_but_never_races_a_live_one(state, settings):
    settings("on")
    sp = os.path.join(str(state), "status.json")
    with io.open(sp, "w", encoding="utf-8") as fh:
        json.dump({"running": False, "workers": []}, fh)
    p = _snap(state, run_id="rI")
    FRES.record_blocked(p, NOW, "backoff", 1, "")
    st = json.load(io.open(sp, encoding="utf-8"))
    assert st["auto_resume"]["last_decision"]["reason"] == "backoff" and st["workers"] == []
    with io.open(sp, "w", encoding="utf-8") as fh:
        json.dump({"running": True}, fh)                # a live coordinator owns this file
    FRES.record_blocked(p, NOW + 1, "backoff", 1, "")
    assert "auto_resume" not in json.load(io.open(sp, encoding="utf-8"))


def test_the_coordinators_status_carries_the_block(state, settings, monkeypatch):
    from relay import fleet_runner as FR
    settings("on")
    monkeypatch.setattr(FR, "_ACTIVE_STATE_DIR", str(state))
    _snap(state, run_id="rS")
    block = FR._auto_resume_block()
    assert block["auto_resume"]["pending_snapshots"] == 1 and block["auto_resume"]["setting"] == "on"
    src = io.open(os.path.join(REPO, "relay", "fleet_runner.py"), encoding="utf-8").read()
    assert "_snap.update(_auto_resume_block())" in src, "_snapshot does not export the block"


# -- the cockpit control --------------------------------------------------------------------------

def _cs(name):
    return io.open(os.path.join(REPO, "ui", name), encoding="utf-8-sig").read()


def test_the_cockpit_view_agrees_with_the_registry():
    cs = _cs("EffortPolicy.cs")
    assert 'public const string Key = "%s";' % FRES.AUTO_RESUME_SETTING_KEY in cs
    m = re.search(r"class AutoResumeView.*?public const string Default = \"(\w+)\";", cs, re.S)
    assert m and m.group(1) == SK.default("fleet_auto_resume")
    assert 'Modes = { "off", "on" }' in cs[cs.index("class AutoResumeView"):]


def test_the_control_lives_in_the_popup_saves_through_savekey_and_does_not_refire():
    src = _cs("FleetCockpit.cs")
    assert "col.Children.Add(AutoResumeControl());" in src
    assert "ctrls.Children.Add(AutoResumeControl());" not in src, "a header control: the header is pinned"
    assert 'SectionHeader(L("復旧 / Recovery", "Recovery"))' in src
    m = re.search(r"UIElement AutoResumeControl\(\).*?\n    }\n", src, re.S)
    assert m and "File." not in m.group(0)
    assert "sel == _arVal) return;" in m.group(0)
    assert "SaveKey(AutoResumeView.Key, _arVal);" in m.group(0)
    assert len(re.findall(r"SaveKey\(AutoResumeView\.Key", src)) == 1
    m = re.search(r"void PaintAutoResume\(\).*?\n    }\n", src, re.S)
    assert m and "!Equals(ComboVal(_arBox), _arVal)" in m.group(0)
    assert "AutoResumeView.ParseLine(ln)" in src, "the setting is not loaded back"
    assert "PaintAutoResumeInEffect(root);" in src, "the status the supervisor reports is not painted"
    m = re.search(r"void PaintAutoResumeInEffect\(.*?\n    }\n", src, re.S)
    assert m and 'Obj(root, "auto_resume")' in m.group(0) and 'Obj(ar, "last_decision")' in m.group(0)


def test_the_panel_names_the_timing_the_registry_declares():
    src = _cs("FleetCockpit.cs")
    m = re.search(r'case "fleet_auto_resume":\s*case "rate_ceiling_rpm":', src)
    assert m, "fleet_auto_resume is not in the each_gate arm of SettingsTiming"
    i = src.index("static string SettingsTiming(string key)")
    arm = src[i:src.index('return "each_gate";', i)]
    assert 'case "fleet_auto_resume":' in arm
