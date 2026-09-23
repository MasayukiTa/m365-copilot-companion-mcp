# -*- coding: utf-8 -*-
"""The startup banner queued behind itself for ~20 minutes on 2026-09-24. This is the backstop.

## What happened (07:35-07:53, read off process trees and .fleet/autofix.jsonl)

(a) FleetCockpit's health-poll auto-heal called RunStartAll() on its OWN first sweep whenever
    server/tunnel read Red -- with no check for whether a start_all.ps1 was ALREADY mid-bring-up,
    including the one that had just launched THIS cockpit process. start_all.ps1 relaunches a
    stale UI while it is still starting the backend, so the new cockpit saw the same Red and
    launched ANOTHER start_all, which queued on start_all's own single-instance lock ("another
    start_all is already running... waiting for it to finish"), then relaunched the cockpit
    again.
(b) The retry budget (AUTOFIX_MAX_ATTEMPTS) and RunStartAll's 120s cooldown lived in that
    process's own memory, so every relaunched cockpit got a FRESH budget -- neither backstop
    the loop in (a) should have hit ever fired.
(c) repair.ps1 -Auto's Tier A repair for server/tunnel is itself another start_all.ps1
    invocation (`-NoUi -NoSplash`), and unparsable repair.ps1 output fell back to a blind
    RunStartAll() -- two more ways the same branch could add a queued startup.

## What this runs

ui/FleetCockpit.cs is WPF and cannot be imported from pytest, so this exercises the extracted
PURE decision classes directly -- `StartupGate.GateAutomaticLaunch` and `AutoFixBudget.Decide`,
both in the SHIPPED ui/SelfImproveDashboard.cs (placed there, not in FleetCockpit.cs, because
that file has no Main and a throwaway console harness can compile it standalone; see FrozenGate's
now-removed placement for precedent). These are the SAME functions FleetCockpit.cs's RunStartAll,
its startup-sweep auto-heal, and MaybeAutoFix call -- not a re-implementation of their policy.

Compiles ui/SelfImproveDashboard.cs + ui/Theme.cs (WPF references: SelfImproveDashboardWindow
extends Window) plus the test-only driver ui/harness/StartupGateHarness.cs with the real csc.
Registered in ui/test_one_list_says_what_the_ui_compiles.py's EXEMPT (compiles a WPF source file
standalone into a throwaway console harness, not a build of either shipped UI binary) and
COMPILED_BY_A_TEST (ui/harness/StartupGateHarness.cs).

## The four scenarios named in the incident review

  1. start_all already running            -> no automatic launch, ever (gate cases).
  2. this cockpit was launched BY start_all -> no FIRST-SWEEP heal, silently (gate cases).
  3. the attempt budget persists across two "processes" -- modelled as two consecutive
     AutoFixBudget.Decide calls where the first call's `Kept` feeds the second call's
     `attempts`, exactly the read-decide-write cycle FleetCockpit.cs's
     TryConsumeAutoFixBudget performs against .fleet/autofix_budget.json (budget_sequence
     cases).
  4. once the budget is exhausted, no launch, and the decision says so (Exhausted=true) so the
     caller can show the plain-language "not a loop" message exactly once.

## What is NOT executed here

The WPF/process wiring: that FleetCockpit.cs actually reads the "Global\\m365-copilot-companion-
start-all" mutex and M365_LAUNCHED_BY_START_ALL, that RunStartAll/RunFix/MaybeAutoFix call these
functions with the right arguments, that .fleet/autofix_budget.json round-trips through real
file I/O, and that the exhaustion message is shown exactly once (`_autoFixExhaustedNoted`).
ui/test_both_windows_can_be_constructed.py proves the cockpit still constructs with these files
in its Build line; the rest is a live GUI/process check.

## Skips

Only on a non-Windows host. On Windows a missing csc skips unless `REQUIRE_CSC=1` (CI), which
fails instead. A compile failure or a harness crash always fails.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")
WPF = os.path.join(FW, "WPF")

SOURCES = [
    os.path.join(UI, "SelfImproveDashboard.cs"),
    os.path.join(UI, "Theme.cs"),
    os.path.join(UI, "harness", "StartupGateHarness.cs"),
]

REFS = [
    os.path.join(WPF, "PresentationFramework.dll"),
    os.path.join(WPF, "PresentationCore.dll"),
    os.path.join(WPF, "WindowsBase.dll"),
    os.path.join(FW, "System.Xaml.dll"),
    os.path.join(FW, "System.Web.Extensions.dll"),
    os.path.join(FW, "System.Windows.Forms.dll"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the code under test is C# compiled by the .NET Framework csc, which exists only "
           "on Windows")


def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("startupgate_build")
    out = str(d / "StartupGateHarness.exe")
    cmd = [CSC, "/nologo", "/target:exe", "/out:" + out] + ["/r:" + r for r in REFS] + SOURCES
    r = childproc.run(cmd, timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (
        "csc could not build SelfImproveDashboard.cs with its harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    return out


def _run(exe_path, tmp_path, verb, cases, name):
    cpath = str(tmp_path / (name + "_cases.json"))
    rpath = str(tmp_path / (name + "_results.json"))
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh)
    r = childproc.run([exe_path, verb, cpath, rpath], timeout=60)
    assert r.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        r.returncode, r.stdout, r.stderr)
    with open(rpath, encoding="utf-8") as fh:
        return json.load(fh)


# ── (1) start_all already running: no automatic launch, ever ───────────────────────────────

def test_start_all_running_blocks_every_automatic_launch(exe, tmp_path):
    cases = [
        {"startAllRunning": True, "launchedByStartAll": False, "isFirstSweep": True},
        {"startAllRunning": True, "launchedByStartAll": False, "isFirstSweep": False},
        {"startAllRunning": True, "launchedByStartAll": True, "isFirstSweep": True},
    ]
    got = _run(exe, tmp_path, "gate", cases, "running")
    for case, row in zip(cases, got):
        assert row["allow"] is False, (case, row)
        assert row["reasonKey"] == "autofix_start_all_running", (
            "start_all running must say so, not fail silently: %r" % row)


# ── (2) launched BY start_all: no FIRST-SWEEP heal, and it is silent (nothing is wrong) ─────

def test_launched_by_start_all_skips_only_the_first_sweep_and_says_nothing(exe, tmp_path):
    cases = [
        {"startAllRunning": False, "launchedByStartAll": True, "isFirstSweep": True},
        {"startAllRunning": False, "launchedByStartAll": True, "isFirstSweep": False},
        {"startAllRunning": False, "launchedByStartAll": False, "isFirstSweep": True},
    ]
    got = _run(exe, tmp_path, "gate", cases, "launched")
    first_sweep, later, not_launched = got
    assert first_sweep["allow"] is False, first_sweep
    assert first_sweep["reasonKey"] == "", (
        "being launched by start_all is not a problem -- it must not surface a message: %r"
        % first_sweep)
    assert later["allow"] is True, (
        "a cockpit start_all launched must still be able to auto-heal later, once start_all "
        "itself is done: %r" % later)
    assert not_launched["allow"] is True, not_launched


# ── (3) the budget persists across two "processes" ──────────────────────────────────────────

def test_the_budget_persists_across_two_processes(exe, tmp_path):
    """One case, two Decide calls chained through Kept -- exactly the read-decide-write cycle
    TryConsumeAutoFixBudget performs against the real json file. If the SECOND call started
    from an empty budget (the bug: a relaunched process getting a fresh field), attempt #2
    here would read as allowed under a maxAttempts=1 cap; it must not."""
    case = {"maxAttempts": 1, "windowS": 1800, "steps": [{"now": 0}, {"now": 1}]}
    got, = _run(exe, tmp_path, "budget_sequence", [case], "two_processes")
    first, second = got["steps"]
    assert first["allow"] is True, first
    assert second["allow"] is False, (
        "the second 'process' saw an empty budget and was allowed to launch again -- this is "
        "bug (b), the exact defect this file exists to catch: %r" % second)
    assert second["exhausted"] is True, second


# ── (4) exhausted -> no launch, and the decision says so (Exhausted=true) ───────────────────

def test_exhausted_never_allows_and_flags_itself(exe, tmp_path):
    case = {"maxAttempts": 3, "windowS": 1800,
            "steps": [{"now": 0}, {"now": 1}, {"now": 2}, {"now": 3}, {"now": 4}, {"now": 5}]}
    got, = _run(exe, tmp_path, "budget_sequence", [case], "exhausted")
    steps = got["steps"]
    assert [s["allow"] for s in steps] == [True, True, True, False, False, False], steps
    assert [s["exhausted"] for s in steps] == [False, False, False, True, True, True], steps


def test_the_window_ages_attempts_out_so_backoff_is_bounded_not_permanent(exe, tmp_path):
    """3 attempts per 30 minutes, not 3 attempts ever: once the oldest attempts fall outside
    the window, the key must be usable again -- 'not a loop' does not mean 'never again'."""
    case = {"maxAttempts": 1, "windowS": 120, "steps": [{"now": 0}, {"now": 50}, {"now": 121}]}
    got, = _run(exe, tmp_path, "budget_sequence", [case], "ages_out")
    first, second, third = got["steps"]
    assert first["allow"] is True
    assert second["allow"] is False, "within the 120s cooldown, a second launch was allowed"
    assert third["allow"] is True, "121s later (outside the window) the key never freed up"


def test_every_case_ran(exe, tmp_path):
    """Fail closed against a harness that silently drops cases."""
    cases = [{"startAllRunning": b, "launchedByStartAll": False, "isFirstSweep": True}
             for b in (True, False)]
    got = _run(exe, tmp_path, "gate", cases, "count")
    assert len(got) == len(cases)
