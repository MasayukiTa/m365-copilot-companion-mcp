"""The reaper has to be runnable, and must never touch a run that is alive.

relay/fleet_reaper.py was complete, covered by nine tests, and referenced by nothing: no
__main__, no main(), no scheduler, no caller anywhere in the repository. A phantom run could
be finalized only by someone who knew the function existed and opened a Python prompt. That
is the same shape as the browser nobody collected and the approval nobody requested, so the
entry point is pinned here along with the invariant that makes it safe to attach to startup.
"""
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "relay", "fleet_reaper.py")


def test_the_module_can_actually_be_run():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "main" in names, "a capability with no entry point cannot be triggered"
    assert '__main__' in open(SRC, encoding="utf-8").read()


def test_running_it_reports_and_exits_cleanly(tmp_path):
    """Report mode must be safe to run anywhere, including where there is no fleet at all."""
    out = subprocess.run([sys.executable, "-m", "relay.fleet_reaper",
                          "--fleet-dir", str(tmp_path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "no active-run marker" in out.stdout


def test_a_live_run_is_never_reaped(tmp_path):
    """THE invariant that lets this be wired into startup: a marker whose pid is this very
    process must be left alone, even with --reap."""
    import json
    (tmp_path / "fleet_run_active.json").write_text(
        json.dumps({"pid": os.getpid(), "start_ts": 0}), encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps({"running": True, "workers": []}),
                                          encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "relay.fleet_reaper",
                          "--fleet-dir", str(tmp_path), "--reap"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "ALIVE" in out.stdout
    assert (tmp_path / "fleet_run_active.json").exists(), "a live run's marker was removed"


def test_startup_sweeps_phantom_runs():
    """Wired where it will actually fire: the launcher people click."""
    src = open(os.path.join(ROOT, "scripts", "start_all.ps1"), encoding="utf-8").read()
    assert "relay.fleet_reaper" in src
