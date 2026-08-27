"""Continuing a run whose coordinator died, and never continuing one that is alive.

Everything this needs already existed and nothing joined it up: the marker carries a
precomputed resume argv, should_auto_resume() states the rule and is tested, and its
docstring names a PowerShell mirror that supervises the MCP server and the dev tunnel and
never mentions the marker. So an interrupted run stayed interrupted, and the way anyone found
out was that the answer never came.

The dangerous direction is the opposite one: relaunching a run that is actually alive puts
two coordinators on one state directory, overwriting each other's status. That is what most
of these pin.
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "resume_interrupted_fleet",
    os.path.join(ROOT, "scripts", "win", "resume_interrupted_fleet.py"))
ri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ri)


def _marker(tmp_path, **kw):
    payload = {"pid": 999999, "resume_argv": ["--state-dir", "C:/x/.fleet", "--effort", "auto"]}
    payload.update(kw)
    (tmp_path / "fleet_run_active.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(tmp_path)


def test_no_marker_means_nothing_to_do(tmp_path, capsys):
    assert ri.main(["--state-dir", str(tmp_path), "--resume"]) == 0
    assert "no interrupted fleet run" in capsys.readouterr().out


def test_a_live_run_is_never_relaunched(tmp_path, monkeypatch, capsys):
    """Two coordinators on one state directory overwrite each other's status.

    pid_alive is stubbed rather than left to the real process table: subprocess.run is itself
    implemented with Popen, so patching Popen to record launches also intercepts the liveness
    probe -- the first version of this test recorded that probe and called it a relaunch.
    """
    d = _marker(tmp_path, pid=os.getpid())
    monkeypatch.setattr(ri, "pid_alive", lambda pid: True)
    launched = []
    monkeypatch.setattr(ri.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    assert ri.main(["--state-dir", d, "--resume"]) == 0
    assert launched == []
    assert "nothing to resume" in capsys.readouterr().out


def test_an_unreadable_process_table_is_treated_as_alive(tmp_path, monkeypatch):
    """On doubt, do not relaunch. A missed resume costs a delay; a double run costs the run."""
    def boom(*a, **k):
        raise OSError("no process table")
    monkeypatch.setattr(ri.subprocess, "run", boom)
    assert ri.pid_alive(4242) is True


def test_a_dead_run_is_relaunched_with_resume(tmp_path, monkeypatch, capsys):
    d = _marker(tmp_path)
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    launched = []
    monkeypatch.setattr(ri.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    assert ri.main(["--state-dir", d, "--resume"]) == 0
    assert len(launched) == 1
    cmd = launched[0]
    assert cmd[1:3] == ["-m", "relay.fleet_runner"]
    assert cmd[-1] == "--resume"


def test_the_original_goals_file_is_not_replayed(tmp_path):
    """--resume rebuilds the goal set from the ledger; replaying the goals file on top adds
    every goal a second time, the finished ones included."""
    cmd = ri.resume_command({"resume_argv": ["--state-dir", "C:/x", "--effort", "auto"]})
    assert "--goals-file" not in cmd
    assert "-g" not in cmd
    assert cmd.count("--resume") == 1


def test_a_marker_without_a_resume_argv_says_so(tmp_path, monkeypatch, capsys):
    """Better a clear refusal naming where the goals are than a guessed command line."""
    d = _marker(tmp_path, resume_argv=[])
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    assert ri.main(["--state-dir", d, "--resume"]) == 1
    assert "ledger" in capsys.readouterr().out


def test_report_mode_launches_nothing(tmp_path, monkeypatch, capsys):
    d = _marker(tmp_path)
    monkeypatch.setattr(ri, "pid_alive", lambda pid: False)
    launched = []
    monkeypatch.setattr(ri.subprocess, "Popen", lambda *a, **k: launched.append(a))
    assert ri.main(["--state-dir", d]) == 0
    assert launched == []
    assert "Would resume" in capsys.readouterr().out


def test_startup_resumes_before_it_reaps():
    """The reaper clears the very marker the resume reads. Wrong order silently loses the run."""
    src = open(os.path.join(ROOT, "scripts", "start_all.ps1"), encoding="utf-8").read()
    assert src.index("resume_interrupted_fleet") < src.index("relay.fleet_reaper")
