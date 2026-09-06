# -*- coding: utf-8 -*-
"""Starting a fleet because a goal arrived and nothing was running.

FLEET_INTAKE_AUTOSTART existed as a flag with an empty branch: setting it changed nothing and
the code said so out loud. The argument for filling it in is the door's own name -- fleet_submit
means "give this to the fleet", so an instruction arriving with no fleet running is a request to
run one, not a request to wait for the owner to notice.

What the flag has to get right is not starting. It is not starting TWICE. Two fleets share one
dedicated Edge and clobber each other's status.json, and the second run's work shows up as a
phantom worker behind the first -- that is on this project's record already. The router drains
every fifteen seconds and a cold start takes minutes, so "no fleet is live" is TRUE for many
passes after a launch. Every test here is about telling those passes apart.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "fleet"
    sd.mkdir()
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(sd))
    monkeypatch.setattr(TR, "AUTOSTART", True)
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://example.invalid/agent")
    TR.ensure_dirs()
    return sd


def _live(sd, running=True):
    with io.open(os.path.join(str(sd), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"running": running}, fh)


class _Launcher:
    """Stands in for Popen. A test that actually started a fleet would open a browser and spend
    Copilot turns, which is the cost this whole design exists to spend deliberately."""

    def __init__(self, pid=4242):
        self.calls = []
        self.pid = pid

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.pid


# -- it starts, and it starts once -------------------------------------------------------------

def test_a_goal_with_no_fleet_running_starts_one(state):
    launcher = _Launcher()
    out = TR.autostart_fleet([{"text": "read yesterday's mail"}], str(state), launcher=launcher)
    assert out["ok"] is True and out["pid"] == 4242
    assert len(launcher.calls) == 1
    cmd = launcher.calls[0]
    assert "relay.fleet_runner" in cmd and "--goals-file" in cmd and "--state-dir" in cmd


def test_the_goal_reaches_the_run_as_its_goals_file(state):
    """One JSON object per line -- the format _read_goals expects. A fleet started with no
    goals exits immediately, so the goal cannot be delivered afterwards."""
    launcher = _Launcher()
    TR.autostart_fleet([{"text": "goal one"}, {"text": "goal two", "priority": True}],
                       str(state), launcher=launcher)
    path = launcher.calls[0][launcher.calls[0].index("--goals-file") + 1]
    rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
    assert [r["text"] for r in rows] == ["goal one", "goal two"]
    assert rows[1]["priority"] is True


def test_a_second_goal_while_the_first_is_still_coming_up_does_not_start_another(state, monkeypatch):
    """THE FAILURE THIS GUARD EXISTS FOR. A cold start takes minutes and the router drains
    every fifteen seconds, so the passes in between all see "no fleet is live"."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: True)
    launcher = _Launcher()
    TR.autostart_fleet([{"text": "first"}], str(state), now=1000.0, launcher=launcher)
    may, why = TR.autostart_status(str(state), now=1030.0)
    assert may is False, why
    assert "coming up" in why


def test_once_the_run_is_live_autostart_has_nothing_to_do(state):
    """fleet_handoff never reaches autostart while a run is live -- the goal joins it instead."""
    _live(state)
    status, result = TR.fleet_handoff("join the running one", "j1", str(state))
    assert status == "dispatched"
    assert result["delivered"] == "add_goal"


# -- and it does not spin ----------------------------------------------------------------------

def test_a_launch_that_never_became_live_is_backed_off_not_retried(state, monkeypatch):
    """Without this the router opens a browser every fifteen seconds for as long as the goal
    waits."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "doomed"}], str(state), now=1000.0, launcher=_Launcher())
    TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)

    may, why = TR.autostart_status(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 2)
    assert may is False, why
    assert "backoff" in why


def test_after_the_backoff_it_tries_again(state, monkeypatch):
    """Backing off forever would be the same outcome as never implementing this."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "doomed"}], str(state), now=1000.0, launcher=_Launcher())
    TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)

    may, _ = TR.autostart_status(str(state), now=1000.0 + TR.AUTOSTART_BACKOFF_S + 10)
    assert may is True


def test_the_goals_of_a_run_that_died_are_put_back(state, monkeypatch):
    """A GOAL THAT VANISHED BECAUSE A BROWSER FAILED TO OPEN READS AS ONE NEVER SENT.

    The goals left for_fleet/ when the launch was made. If that run never came up they are in
    the goals file of a process that is gone, so they are written into the autostart record
    too, and restored from it here.
    """
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "must not vanish"}], str(state), now=1000.0,
                       launcher=_Launcher())
    restored = TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)

    assert len(restored) == 1
    waiting = os.listdir(os.path.join(TR.TASKS, "for_fleet"))
    assert len(waiting) == 1
    with io.open(os.path.join(TR.TASKS, "for_fleet", waiting[0]), encoding="utf-8") as fh:
        assert fh.read() == "must not vanish"


def test_a_run_that_did_come_up_keeps_its_goals(state, monkeypatch):
    """Restoring them then would deliver everything twice."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "already running"}], str(state), now=1000.0,
                       launcher=_Launcher())
    _live(state)
    restored = TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)
    assert restored == []
    assert os.listdir(os.path.join(TR.TASKS, "for_fleet")) == []


def test_a_process_still_alive_is_left_alone_however_long_it_takes(state, monkeypatch):
    """Sign-in can be slow. A runner that is still there has not failed, whatever the clock
    says, and killing its goals out from under it would be the worse mistake."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: True)
    TR.autostart_fleet([{"text": "slow start"}], str(state), now=1000.0, launcher=_Launcher())
    assert TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S * 5) == []


def test_an_unknown_pid_state_counts_as_alive(state, monkeypatch):
    """The only thing _pid_alive gates is whether to launch ANOTHER fleet, and two fleets on
    one Edge is the failure the check exists to prevent. Unknown must not read as permission."""
    def explode(*a, **k):
        raise OSError("tasklist unavailable")
    monkeypatch.setattr(TR.subprocess, "run", explode)
    assert TR._pid_alive(12345) is True


# -- the refusals ------------------------------------------------------------------------------

def test_with_the_flag_off_nothing_starts(state, monkeypatch):
    monkeypatch.setattr(TR, "AUTOSTART", False)
    may, why = TR.autostart_status(str(state))
    assert may is False and "autostart is off" in why


def test_with_no_agent_url_it_says_so_rather_than_launching_nothing(state, monkeypatch):
    monkeypatch.delenv("MCP_FLEET_AGENT_URL", raising=False)
    monkeypatch.delenv("MCP_IMPL_AGENT_URL", raising=False)
    launcher = _Launcher()
    out = TR.autostart_fleet([{"text": "x"}], str(state), launcher=launcher)
    assert out["ok"] is False and "agent URL" in out["detail"]
    assert launcher.calls == []


def test_an_empty_goal_list_starts_nothing(state):
    launcher = _Launcher()
    out = TR.autostart_fleet([{"text": ""}], str(state), launcher=launcher)
    assert out["ok"] is False
    assert launcher.calls == []


def test_a_launch_that_raises_is_recorded_and_not_reported_as_started(state):
    def boom(cmd):
        raise OSError("no python here")
    out = TR.autostart_fleet([{"text": "x"}], str(state), launcher=boom)
    assert out["ok"] is False and "no python here" in out["detail"]
    assert TR._read_autostart(str(state))["outcome"] == "launch_failed"


def test_the_handoff_says_which_way_the_goal_went(state):
    """dispatched-by-autostart and dispatched-by-add_goal are different events, and the record
    is the only place the difference survives."""
    TR.autostart_fleet([{"text": "seed"}], str(state), now=1.0, launcher=_Launcher())
    _live(state)
    _, result = TR.fleet_handoff("later goal", "j2", str(state))
    assert result["delivered"] == "add_goal"


def test_a_launch_whose_process_died_early_is_not_relaunched_at_once(state, monkeypatch):
    """The gap between a crash and the pass that notices it.

    recover_failed_autostart only writes never_became_live once the grace period is up. Between
    a launch dying at second three and that pass, autostart_status must not answer "nothing in
    flight" -- that is a fresh browser every fifteen seconds against whatever killed the first.
    """
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "dies at once"}], str(state), now=1000.0, launcher=_Launcher())
    may, why = TR.autostart_status(str(state), now=1003.0)
    assert may is False, why
    assert "backoff" in why


def test_a_live_fleet_stops_autostart_before_any_record_is_consulted(state):
    """Belt and braces: fleet_handoff never reaches autostart while a run is live, but nothing
    should depend on that being the only caller."""
    _live(state)
    may, why = TR.autostart_status(str(state))
    assert may is False and "already running" in why


# -- the settings have to reach the process that reads them -------------------------------------

def _in_subprocess(code, env=None):
    """Run `code` in a fresh interpreter, the way the supervisor runs the router."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    e = dict(os.environ) if env is None else env
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=repo, env=e)
    return (r.stdout or "").strip(), (r.stderr or "").strip()


def test_the_router_reads_the_operators_env_file():
    """A SETTING THAT NEVER REACHES THE BRANCH IT NAMES IS NOT A SETTING.

    Every constant in task_router comes from os.environ, and the module runs as a fresh
    subprocess: the supervisor launches `python relay/task_router.py --once` on each pass, into
    an environment nobody had put .env into. Measured before this was wired: AUTOSTART False
    and no agent URL, with both configured in .env. FLEET_INTAKE_AUTOSTART could have been set
    to 1 and changed nothing -- the third flag in one night that did not reach its own branch.

    Skipped where there is no .env (CI), because the property under test is that the file is
    read, and there is nothing to read.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    envfile = os.path.join(repo, ".env")
    if not os.path.isfile(envfile):
        pytest.skip("no .env in this checkout")
    with io.open(envfile, encoding="utf-8-sig", errors="replace") as fh:
        keys = [l.split("=", 1)[0].strip() for l in fh if "=" in l and not l.strip().startswith("#")]
    key = next((k for k in ("MCP_FLEET_AGENT_URL", "MCP_IMPL_AGENT_URL") if k in keys), None)
    if not key:
        pytest.skip("this .env sets no agent URL to check with")

    clean = {k: v for k, v in os.environ.items() if k not in ("MCP_FLEET_AGENT_URL",
                                                              "MCP_IMPL_AGENT_URL")}
    out, err = _in_subprocess(
        "import sys; sys.path.insert(0, '.');"
        "from relay import task_router as TR; print('URL' if TR._agent_url() else 'EMPTY')",
        env=clean)
    assert out == "URL", ("the router did not pick %s up from .env: %s / %s" % (key, out, err))


def test_an_exported_variable_still_beats_the_file():
    """override=False, and it matters here more than most places: conftest points
    FLEET_STATE_DIR at a temp directory for every test run, and a file on disk must not be able
    to take that back -- task_router delivers goals into <state_dir>/commands.d, so losing the
    redirect would hand the operator's live fleet a goal from a test."""
    marked = dict(os.environ, FLEET_STATE_DIR=r"C:\somewhere\deliberate")
    out, err = _in_subprocess(
        "import sys; sys.path.insert(0, '.');"
        "from relay import task_router as TR; print(TR.FLEET_STATE_DIR)", env=marked)
    assert out.endswith("deliberate"), ("the .env file overrode an exported variable: %s / %s"
                                        % (out, err))


def test_no_test_can_start_a_real_fleet(monkeypatch, tmp_path):
    """THE GUARD THAT BECAME NECESSARY THE MOMENT THE FLAG WENT INTO .env.

    task_router reads .env at import, so with FLEET_INTAKE_AUTOSTART=1 every test that imports
    it inherited AUTOSTART True, and any test reaching fleet_handoff with no live fleet would
    have spawned a real run: a browser, workers, and the tenant's Copilot budget. Two layers
    answer that -- conftest clears the ambient value, and this refusal catches the test the
    fixture missed. Checked with AUTOSTART forced back on, which is the state the fixture is
    protecting against.
    """
    monkeypatch.setattr(TR, "AUTOSTART", True)
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://example.invalid/agent")
    sd = tmp_path / "fleet"
    sd.mkdir()
    out = TR.autostart_fleet([{"text": "must not open a browser"}], str(sd))
    assert out["ok"] is False and "under pytest" in out["detail"]


def test_the_ambient_flag_is_off_for_every_test():
    """What conftest's autouse fixture is for. Without it this reads whatever .env says on the
    machine the suite happens to run on, which is not a property of the code."""
    assert TR.AUTOSTART is False


# ---------------------------------------------------------------------------------------
# A RUN THAT FINISHED IS NOT A RUN THAT NEVER STARTED.
#
# recover_failed_autostart asked fleet_is_live -- "is one running NOW" -- and for a run that
# completed inside the grace window the answer is no, for exactly the same reason it is no
# when the launch failed: the fleet is gone and the pid has exited. So a successful run was
# filed as never_became_live and its goal handed back to the queue, to be done a second time.
#
# Found by running the first real autostart rather than by reading the code: launched
# 15:14:19, DONE at 15:17:18, grace up at 15:18:19, and at 15:18:31 the goal it had already
# completed reappeared in for_fleet/. AUTOSTART_GRACE_S is 240s, so every run shorter than
# four minutes hit this -- which is most small goals.
# ---------------------------------------------------------------------------------------

def _write_status(state, started, running=False):
    """The status.json a real fleet_runner writes: its own start stamp, and whether it is up."""
    io.open(os.path.join(str(state), "status.json"), "w", encoding="utf-8").write(
        json.dumps({"started": started, "running": running, "total": 1, "done_count": 1}))


def test_a_run_that_finished_inside_the_grace_is_not_treated_as_never_started(state, monkeypatch):
    """THE regression. The run came up, did the work and exited, all before the grace expired."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)     # a finished run has no pid
    TR.autostart_fleet([{"text": "small goal"}], str(state), now=1000.0, launcher=_Launcher())
    _write_status(state, started=1003.0, running=False)          # started after us, now done

    restored = TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)

    assert restored == [], "a completed run's goal was handed back to the queue"
    assert TR._read_autostart(str(state))["outcome"] == "became_live"


def test_a_launch_that_truly_never_came_up_still_restores_its_goal(state, monkeypatch):
    """The behaviour the fix must not cost: with no run of ours ever recorded, the goal is
    still recoverable. A stale status.json from an EARLIER run must not rescue this launch."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    _write_status(state, started=900.0, running=False)           # a PREVIOUS run, before us
    TR.autostart_fleet([{"text": "doomed"}], str(state), now=1000.0, launcher=_Launcher())

    restored = TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)

    assert len(restored) == 1
    assert TR._read_autostart(str(state))["outcome"] == "never_became_live"


def test_no_status_file_at_all_still_restores(state, monkeypatch):
    """The first-ever autostart on a fresh machine: nothing has written status.json."""
    monkeypatch.setattr(TR, "_pid_alive", lambda pid: False)
    TR.autostart_fleet([{"text": "doomed"}], str(state), now=1000.0, launcher=_Launcher())
    assert not os.path.isfile(os.path.join(str(state), "status.json"))

    assert len(TR.recover_failed_autostart(str(state), now=1000.0 + TR.AUTOSTART_GRACE_S + 1)) == 1


def test_run_started_since_reads_the_start_stamp_not_the_running_flag(state):
    """The distinction the fix rests on, asserted directly: a stopped run still answers yes."""
    _write_status(state, started=1003.0, running=False)
    assert TR._run_started_since(str(state), 1000.0) is True     # finished, but it DID start
    assert TR._run_started_since(str(state), 1100.0) is False    # belongs to an earlier launch
