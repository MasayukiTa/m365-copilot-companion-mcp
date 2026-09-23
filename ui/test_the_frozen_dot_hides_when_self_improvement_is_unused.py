# -*- coding: utf-8 -*-
"""The health strip's self-improvement-check dot must not exist on a machine that has never
touched self-improvement, and must never read green (or hidden) as a way of dodging a real
mismatch on a machine that DOES use it.

## The complaint that started this

"今なぜか凍結セットの信号が付くようになったけどこれなぜいれたの?" -- the 7th health dot
(hs_frozen, added 2026-09-19) reads NO_BASELINE -> RED on any machine with no
relay/selfimprove/frozen_baseline.json anchor, which is every machine except the one running
the self-improvement loop. The baseline JSON itself is tracked in git and so exists on every
checkout; what does NOT exist on an ordinary machine is `~/.selfimprove_frozen_anchor`, written
only by frozen.py's snapshot/re-sign path (by hand from the dashboard, or by the loop's own
re-sign cycle) -- see SelfImproveDashboardWindow.SelfImproveInUse() in ui/SelfImproveDashboard.cs.
A standing red light nobody on that machine can act on is the defect; the jargon (covered by
ui/test_the_frozen_dot_has_no_untranslated_jargon.py) is the second one.

## What this runs

FleetCockpit.cs / SelfImproveDashboard.cs are WPF and cannot be imported from pytest, so this
exercises the extracted PURE decision function directly: `SelfImproveDashboardWindow.FrozenGate.
Decide(inUse, ok, drift)` in ui/SelfImproveDashboard.cs, which is the SAME function FleetCockpit.
cs's health-poll loop calls to color dot 6 (see the "6) Frozen set" block in PollHealthOnce) --
not a re-implementation of its policy. It is a pure function of three already-computed facts, so
this drives it directly rather than staging real baseline/anchor files on disk, the same
trade-off ui/test_a_submitted_task_is_on_top_at_once.py makes for SubmittedTasks.Compose.

Compiles ui/SelfImproveDashboard.cs + ui/Theme.cs + the test-only driver
ui/harness/FrozenGateHarness.cs with the real csc (WPF references: SelfImproveDashboardWindow
extends Window). Registered in ui/test_one_list_says_what_the_ui_compiles.py's EXEMPT (this
compiles a WPF source file standalone into a throwaway console harness, not a build of either
shipped UI binary) and COMPILED_BY_A_TEST (ui/harness/FrozenGateHarness.cs).

## What is NOT executed here

The WPF wiring: that BuildHealthStrip collapses `_healthDotWrap[6]` and ApplyHealthToUi keeps it
collapsed every sweep, that `_frozenApplicable` is read from `SafeSelfImproveInUse()` before
`FrozenGate.Decide` is even called, and that PollHealthOnce feeds FrozenMatches' real ok/drift
into it. ui/test_both_windows_can_be_constructed.py proves the cockpit still constructs with
these files in its Build line; the rest is a live GUI check.

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

#: Compiled together and nothing else: the shipped file under test (which carries FrozenGate),
#: the shared theme it references, and the test-only driver.
SOURCES = [
    os.path.join(UI, "SelfImproveDashboard.cs"),
    os.path.join(UI, "Theme.cs"),
    os.path.join(UI, "harness", "FrozenGateHarness.cs"),
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
    d = tmp_path_factory.mktemp("frozengate_build")
    out = str(d / "FrozenGateHarness.exe")
    cmd = [CSC, "/nologo", "/target:exe", "/out:" + out] + ["/r:" + r for r in REFS] + SOURCES
    r = childproc.run(cmd, timeout=300)
    assert r.returncode == 0 and os.path.isfile(out), (
        "csc could not build SelfImproveDashboard.cs with its harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    return out


def _decide(exe_path, tmp_path, cases, name):
    cpath = str(tmp_path / (name + "_cases.json"))
    rpath = str(tmp_path / (name + "_results.json"))
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh)
    r = childproc.run([exe_path, "run", cpath, rpath], timeout=60)
    assert r.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        r.returncode, r.stdout, r.stderr)
    with open(rpath, encoding="utf-8") as fh:
        return json.load(fh)


# ── (a) the master switch: not in use => hidden, no matter what the comparison says ────────

NOT_IN_USE_CASES = [
    {"inUse": False, "ok": True, "drift": []},                       # would-be green, suppressed
    {"inUse": False, "ok": False, "drift": ["NO_BASELINE"]},         # would-be red, suppressed
    {"inUse": False, "ok": False, "drift": ["a.py", "b.py"]},        # would-be yellow, suppressed
]


def test_not_in_use_is_always_hidden_and_gray_regardless_of_the_comparison(exe, tmp_path):
    got = _decide(exe, tmp_path, NOT_IN_USE_CASES, "not_in_use")
    assert len(got) == len(NOT_IN_USE_CASES)
    for case, row in zip(NOT_IN_USE_CASES, got):
        assert row["visible"] is False, (case, row)
        assert row["color"] == "gray", (case, row)
        assert row["detailKey"] == "hs_frozen_not_in_use", (case, row)


# ── (b) the safety property: in use + no baseline must not read green, and must not hide ───

def test_in_use_with_no_baseline_is_visible_and_red_never_green_or_hidden(exe, tmp_path):
    case = {"inUse": True, "ok": False, "drift": ["NO_BASELINE"]}
    row, = _decide(exe, tmp_path, [case], "no_baseline")
    assert row["visible"] is True, row
    assert row["color"] == "red", row
    assert row["color"] != "green", "a missing baseline on a machine that IS in use read GREEN"
    assert row["detailKey"] == "hs_frozen_none", row


# ── (c) the other two in-use outcomes, so the whole policy is pinned down ───────────────────

def test_in_use_and_matching_is_visible_and_green(exe, tmp_path):
    case = {"inUse": True, "ok": True, "drift": []}
    row, = _decide(exe, tmp_path, [case], "matching")
    assert (row["visible"], row["color"], row["detailKey"]) == (True, "green", "hs_frozen_ok")


def test_in_use_and_drifting_is_visible_and_yellow(exe, tmp_path):
    case = {"inUse": True, "ok": False, "drift": ["relay/selfimprove/guards.py"]}
    row, = _decide(exe, tmp_path, [case], "drifting")
    assert (row["visible"], row["color"], row["detailKey"]) == (True, "yellow", "hs_frozen_drift")


def test_every_visible_color_is_one_of_the_known_four(exe, tmp_path):
    """Guards the JSON contract itself: a typo'd color string would silently fail to map to any
    HealthState in FleetCockpit.cs's fst ternary and default to Gray there -- exactly the
    "hides the real state" failure this whole file exists to catch."""
    cases = NOT_IN_USE_CASES + [
        {"inUse": True, "ok": True, "drift": []},
        {"inUse": True, "ok": False, "drift": ["NO_BASELINE"]},
        {"inUse": True, "ok": False, "drift": ["x"]},
    ]
    got = _decide(exe, tmp_path, cases, "all_colors")
    assert {row["color"] for row in got} <= {"gray", "green", "yellow", "red"}
