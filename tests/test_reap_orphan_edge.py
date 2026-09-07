"""The reaper's decision rule: stop a managed Edge only when its owner is gone.

The rule is one line, but it is the line that decides whether a live fleet run keeps its
browser. So the cases below are written as the questions somebody would ask before trusting
it to run unattended, and the first of them is the one that must never be got wrong.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "reap_orphan_edge", os.path.join(ROOT, "scripts", "win", "reap_orphan_edge.py"))
reap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reap)


@pytest.fixture
def world(monkeypatch):
    """Describe a machine: which profiles have a browser, and which owners are alive."""
    state = {"procs": {}, "alive": set()}

    def browser_procs(profile):
        return state["procs"].get(profile, (0, 0))

    def owner_alive(pattern):
        return any(name in pattern for name in state["alive"])

    monkeypatch.setattr(reap, "browser_procs", browser_procs)
    monkeypatch.setattr(reap, "owner_alive", owner_alive)
    return state


def test_a_live_fleet_run_keeps_its_browser(world):
    """THE invariant. Reaping this would kill every worker's turn mid-flight."""
    world["procs"]["copilot-companion-edge"] = (9, 579)
    world["alive"].add("fleet_runner")
    rows = reap.survey()
    assert [r["orphan"] for r in rows] == [False]
    assert rows[0]["owner_alive"] is True


def test_the_measurement_browser_is_an_orphan_once_the_series_ends(world):
    """The 2026-08-26 case: 331 MB and a Copilot tab, hours after the series finished."""
    world["procs"]["copilot-eval-edge"] = (10, 331)
    rows = reap.survey()
    assert [r["orphan"] for r in rows] == [True]
    assert "measurement" in rows[0]["owner"]


def test_the_series_still_running_protects_its_browser(world):
    world["procs"]["copilot-eval-edge"] = (10, 331)
    world["alive"].add("run_transport_series")
    assert [r["orphan"] for r in reap.survey()] == [False]


def test_the_other_measurement_script_counts_as_an_owner_too(world):
    """Two scripts start this browser; either one being alive means it is in use."""
    world["procs"]["copilot-eval-edge"] = (10, 331)
    world["alive"].add("diag_warmup_bias")
    assert [r["orphan"] for r in reap.survey()] == [False]


def test_the_bridge_is_exempt_by_default_because_it_is_supervised(world):
    """Reaping a supervised browser does not reclaim anything -- the supervisor restores it."""
    world["procs"]["copilot-bridge-edge"] = (8, 367)
    rows = reap.survey()
    assert rows[0]["exempt"] is True
    assert rows[0]["orphan"] is False


def test_the_bridge_can_be_reaped_when_asked(world):
    """--include-bridge, for when the supervisor itself is gone."""
    world["procs"]["copilot-bridge-edge"] = (8, 367)
    rows = reap.survey(include_bridge=True)
    assert rows[0]["exempt"] is False
    assert rows[0]["orphan"] is True


def test_a_supervised_bridge_with_its_owner_alive_is_never_an_orphan(world):
    world["procs"]["copilot-bridge-edge"] = (8, 367)
    world["alive"].add("copilot_bridge")
    assert [r["orphan"] for r in reap.survey(include_bridge=True)] == [False]


def test_profiles_with_no_browser_are_not_reported(world):
    """Nothing running is not a finding; a report full of zeroes stops being read."""
    world["alive"].add("fleet_runner")
    assert reap.survey() == []


def test_each_profile_is_judged_on_its_own_owner(world):
    """A live fleet must not make the idle measurement browser look busy."""
    world["procs"]["copilot-companion-edge"] = (9, 579)
    world["procs"]["copilot-eval-edge"] = (10, 331)
    world["alive"].add("fleet_runner")
    by_profile = {r["profile"]: r["orphan"] for r in reap.survey()}
    assert by_profile == {"copilot-companion-edge": False, "copilot-eval-edge": True}


def test_report_mode_stops_nothing(world, monkeypatch, capsys):
    """The default must be safe to run anywhere, including on somebody else's machine."""
    world["procs"]["copilot-eval-edge"] = (10, 331)
    stopped = []
    monkeypatch.setattr(reap, "stop_profile", lambda p: stopped.append(p))
    assert reap.main([]) == 0
    assert stopped == []
    assert "Re-run with --stop" in capsys.readouterr().out


def test_stop_mode_stops_only_the_orphan(world, monkeypatch, capsys):
    world["procs"]["copilot-companion-edge"] = (9, 579)
    world["procs"]["copilot-eval-edge"] = (10, 331)
    world["alive"].add("fleet_runner")
    stopped = []
    monkeypatch.setattr(reap, "stop_profile", lambda p: (stopped.append(p), 10)[1])
    assert reap.main(["--stop"]) == 0
    assert stopped == ["copilot-eval-edge"]
    assert "579" not in capsys.readouterr().out.split("stopped")[-1]


def test_the_rule_reads_nothing_but_the_process_table():
    """A guarantee that needs a state file is not one on a device that lacks the file.

    Checked against the source because the point is the absence of a dependency: the module
    must not learn ownership from .fleet, a marker, or an env var that only this machine has.

    Parsed rather than grepped. A plain text scan matched this module's own prose -- the
    docstring above says the words ".fleet state" while explaining that it does not read
    it -- so the first version of this test failed on the sentence promising the property it
    was testing for. Docstrings are skipped and only real code is inspected.
    """
    import ast

    tree = ast.parse(open(os.path.join(ROOT, "scripts", "win", "reap_orphan_edge.py"),
                          encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    reads_env, opens_files, literals = [], [], []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            reads_env.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            opens_files.append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ".fleet" in node.value or "edge_mode_" in node.value:
                literals.append((node.lineno, node.value[:40]))

    assert not reads_env, "ownership must not depend on env vars (lines %s)" % reads_env
    assert not opens_files, "ownership must not depend on files (lines %s)" % opens_files
    assert not literals, "ownership must not depend on local state (%s)" % literals


def test_every_managed_profile_has_an_owner():
    """A profile added without an owner is invisible to the reaper -- the exact way the eval
    Edge came to have nobody responsible for it in the first place."""
    from relay import edge_recover
    for profile in edge_recover.MANAGED_EDGE_PROFILES.values():
        assert profile in reap.OWNERS, "%s has no declared owner" % profile


# --- Added 2026-09-07: the two ways a naive process match kills a HEALTHY browser. ---

def test_owner_alive_fails_CLOSED_when_powershell_cannot_be_run(monkeypatch):
    """If the ownership check itself cannot run, we must NOT conclude the owner is gone.

    The original _ps() returned "" on any PowerShell failure, indistinguishable from a
    query that ran and found nothing. owner_alive() read that "" as 0 owners -> orphan,
    and under --stop would kill a live run's browser on a transient PowerShell hiccup.
    _ps() now returns the _PS_FAILED sentinel on failure and owner_alive() treats it as
    'assume alive, do not reap'.
    """
    monkeypatch.setattr(reap, "_ps", lambda script, timeout=40: reap._PS_FAILED)
    assert reap.owner_alive("fleet_runner") is True


def test_owner_alive_reads_a_real_zero_as_gone(monkeypatch):
    """A query that actually ran and returned 0 still means the owner is gone -- fail-closed
    must not become 'never reap anything'."""
    monkeypatch.setattr(reap, "_ps", lambda script, timeout=40: "0\n")
    assert reap.owner_alive("fleet_runner") is False


def test_owner_alive_reads_a_positive_count_as_alive(monkeypatch):
    monkeypatch.setattr(reap, "_ps", lambda script, timeout=40: "2\n")
    assert reap.owner_alive("fleet_runner") is True


def test_browser_procs_fails_safe_to_zero_when_powershell_cannot_be_run(monkeypatch):
    """An unreadable process table reports nothing running, not garbage -- and 'nothing
    running' is never treated as an orphan (survey skips profiles with no procs)."""
    monkeypatch.setattr(reap, "_ps", lambda script, timeout=40: reap._PS_FAILED)
    assert reap.browser_procs("copilot-eval-edge") == (0, 0)


def test_profile_match_is_anchored_to_the_user_data_dir_flag():
    """The regex must match a real --user-data-dir=...\\<profile> and refuse an incidental
    mention of the profile name elsewhere on the command line (a tab URL, an extension
    path), which under --stop would be a browser stopped by coincidence."""
    import re as _re
    rx = _re.compile(reap._profile_dir_regex("copilot-eval-edge"))

    # real launches: quoted and bare, both slash styles, child process inherits the flag
    assert rx.search(r'msedge.exe --user-data-dir="C:\\Users\\x\\copilot-eval-edge" --foo')
    assert rx.search(r'msedge.exe --user-data-dir=C:\\Users\\x\\copilot-eval-edge')
    assert rx.search(r'msedge.exe --user-data-dir=C:/Users/x/copilot-eval-edge --type=gpu')

    # strangers: profile name present, but NOT as the user-data-dir path segment
    assert not rx.search(r'msedge.exe --user-data-dir=C:\\Users\\x\\other https://copilot-eval-edge.example.com')
    assert not rx.search(r'msedge.exe https://site/copilot-eval-edge/page')
    # a different profile's Edge must not match this profile
    assert not rx.search(r'msedge.exe --user-data-dir=C:\\Users\\x\\copilot-companion-edge')


def test_profile_regex_escapes_metacharacters():
    """A future profile name with regex metacharacters must be matched literally, not as a
    pattern -- the name is escaped before it goes into the .NET regex."""
    import re as _re
    rx = _re.compile(reap._profile_dir_regex("a.b+c"))
    assert rx.search(r'--user-data-dir=C:\\d\\a.b+c')
    assert not rx.search(r'--user-data-dir=C:\\d\\aXbbc')  # '.' and '+' must be literal
