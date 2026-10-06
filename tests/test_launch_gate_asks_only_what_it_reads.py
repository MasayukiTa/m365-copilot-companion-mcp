"""The launch gate runs before every fleet run; it must not pay for figures it throws away.

Measured 2026-09-24: _launch_blockers took 12.9 s, 9.3 s of it two PowerShell CIM queries for
the private memory of each managed browser -- which only extras["memory"] carries, and which
_launch_blockers discards. That sat between a goal's submission and its first turn on every run.

Hermetic: PowerShell is replaced by a recorder, and loopback connects are refused, so nothing
here touches a real browser.
"""
import json
import os
import socket
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EDGE_ROWS = [
    {"CommandLine": "msedge.exe --user-data-dir=C:\\x\\copilot-companion-edge --headless"},
    {"CommandLine": "msedge.exe --user-data-dir=C:\\x\\copilot-bridge-edge --headless"},
]
MEM_ROWS = [
    {"pid": 1, "cmd": EDGE_ROWS[0]["CommandLine"], "priv": 100 * 1048576.0},
    {"pid": 2, "cmd": EDGE_ROWS[1]["CommandLine"], "priv": 40 * 1048576.0},
]


@pytest.fixture()
def powershell(monkeypatch):
    """Record every PowerShell script and answer the two queries these modules make."""
    import tools.childproc as cp

    scripts = []

    def fake_run(argv, timeout=None, **kw):
        script = argv[-1] if argv else ""
        scripts.append(script)
        if "WorkingSetPrivate" in script:
            out = json.dumps(MEM_ROWS)
        elif "msedge.exe" in script:
            out = json.dumps(EDGE_ROWS)
        else:
            out = ""
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(cp, "run", fake_run)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, addr: 10061)
    return scripts


def _memory_queries(scripts):
    return [s for s in scripts if "WorkingSetPrivate" in s]


def test_the_launch_gate_does_not_measure_browser_memory(powershell):
    from relay import fleet_runner

    blockers = fleet_runner._launch_blockers()
    assert isinstance(blockers, list)
    assert any("msedge.exe" in s for s in powershell), "the gate never asked about windows"
    assert _memory_queries(powershell) == [], (
        "the launch gate paid for %d memory queries it discards" % len(_memory_queries(powershell)))


def test_the_screen_still_gets_every_browser_from_one_sample(powershell):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "checkpoint_mem", os.path.join(ROOT, "scripts", "win", "checkpoint.py"))
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    state = cp.edge_state()
    assert state["copilot-companion-edge"]["mb"] == 100.0
    assert state["copilot-bridge-edge"]["mb"] == 40.0
    assert len(_memory_queries(powershell)) == 1, "one PowerShell sample per browser, again"
