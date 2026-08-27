"""An interrupted fleet run has to be visible.

fleet_run_active.json holds the coordinator's pid and is cleared on clean completion, so a
marker whose pid is dead means a run stopped mid-flight. should_auto_resume() decides whether
such a run should be relaunched, and its docstring says scripts/supervisor.ps1 mirrors the
rule -- that script supervises the MCP server and the dev tunnel and never mentions the
marker. Nothing anywhere resumes one.

Resuming work unprompted is a behaviour change rather than a repair, so the verifier reports
the state instead of acting on it. What these pin is that it reports it HONESTLY: the one way
this check could do harm is by accusing a live run of being dead.
"""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "verify_stack", os.path.join(ROOT, "scripts", "win", "verify_stack.py"))
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def test_no_marker_is_no_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "REPO", str(tmp_path))
    os.makedirs(os.path.join(tmp_path, ".fleet"), exist_ok=True)
    assert vs._active_marker() is None


def test_a_live_run_is_reported_alive(tmp_path, monkeypatch):
    """THE one that must not be got wrong: calling a live run dead would send somebody to
    reap the sidecars of a run that is still writing them."""
    monkeypatch.setattr(vs, "REPO", str(tmp_path))
    d = os.path.join(tmp_path, ".fleet")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet_run_active.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "argv": ["--goals-file", "g.jsonl"]}, fh)
    m = vs._active_marker()
    assert m["alive"] is True
    assert m["pid"] == os.getpid()


def test_a_dead_run_is_reported_dead_with_its_goals(tmp_path, monkeypatch):
    """The liveness probe is stubbed rather than aimed at a pid nobody is using.

    It shells out to PowerShell, which does not exist on the Linux runner CI uses -- and the
    probe treats a failure to ask as "alive", correctly, so on CI this test would have been
    asserting the opposite of what it reads. Local-green-CI-red, from a test that looked
    perfectly ordinary."""
    monkeypatch.setattr(vs, "REPO", str(tmp_path))
    d = os.path.join(tmp_path, ".fleet")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet_run_active.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": 999999, "argv": ["--goals-file", "C:/x/goals.jsonl"]}, fh)

    class _Dead:
        stdout = "0"

    monkeypatch.setattr(vs.subprocess, "run", lambda *a, **k: _Dead())
    m = vs._active_marker()
    assert m["alive"] is False
    assert m["goals"] == "C:/x/goals.jsonl"


def test_a_corrupt_marker_is_not_a_finding(tmp_path, monkeypatch):
    """A broken sidecar is not evidence of an interrupted run."""
    monkeypatch.setattr(vs, "REPO", str(tmp_path))
    d = os.path.join(tmp_path, ".fleet")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet_run_active.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert vs._active_marker() is None


def test_an_unreadable_process_table_never_accuses(tmp_path, monkeypatch):
    """When liveness cannot be determined, the safe answer is 'alive'."""
    monkeypatch.setattr(vs, "REPO", str(tmp_path))
    d = os.path.join(tmp_path, ".fleet")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "fleet_run_active.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242}, fh)

    def boom(*a, **k):
        raise OSError("no process table")

    monkeypatch.setattr(vs.subprocess, "run", boom)
    assert vs._active_marker()["alive"] is True
