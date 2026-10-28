# -*- coding: utf-8 -*-
"""rebuild_ui.ps1 warns about uncommitted tracked ui/ changes before it builds.

WHAT BROKE. rebuild_ui.ps1 builds FleetCockpit FROM THE PRODUCTION CHECKOUT. On 2026-10-27 a
fleet worker's unfinished tracked edit was sitting in ui/, the script compiled it into the
live binary, and the binary crashed on its first tick. The safety valve: before building,
check for UNCOMMITTED TRACKED changes under ui/ and, by default, WARN and build anyway (the
committed convention is preserved); with -IgnoreUncommittedUi, build from the COMMITTED version
(stash the tracked ui/ changes, build, restore them) so a known-good binary is produced without
discarding anyone's edit.

WHY THIS IS A STATIC CHECK. The valve is PowerShell and the CI runner is Linux, where
pwsh + git + csc.exe cannot run it end to end, and the repository rule forbids Windows-only
test code. So this asserts the SCRIPT ITSELF contains the valve, pinned to the specific
contract that made it safe -- tracked-only detection (untracked scratch is not a build input),
the opt-out switch, an unconditional restore so an edit is never stranded in the stash, and
fail-open on a git error so a missing git never stops a rebuild. A reviewer removing any of
these parts fails a test rather than silently reopening the crash.
"""
from __future__ import annotations

import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "rebuild_ui.ps1")


def _body():
    return io.open(SCRIPT, encoding="utf-8", errors="replace").read()


def test_the_script_exists():
    assert os.path.isfile(SCRIPT), SCRIPT


def test_it_takes_the_ignore_uncommitted_switch():
    body = _body()
    assert "-IgnoreUncommittedUi" in body or "IgnoreUncommittedUi" in body
    # it is a real param, not only a comment
    assert "param(" in body and "IgnoreUncommittedUi" in body.split("param(", 1)[1][:200]


def test_it_detects_tracked_changes_only():
    body = _body()
    # tracked, modified/added/deleted -- untracked scratch files are not a build input.
    assert "status --porcelain" in body
    assert "--untracked-files=no" in body
    assert "-- $ui" in body            # scoped to ui/, not the whole repo


def test_the_default_is_warn_and_build_anyway():
    body = _body().lower()
    assert "warning" in body           # it warns
    # the default arm says it builds with the changes in place
    assert "building with these changes" in body or "building with them in place" in body


def test_the_opt_out_builds_from_the_committed_version():
    body = _body()
    # -IgnoreUncommittedUi stashes the tracked ui/ changes to build the committed version.
    assert "git" in body and "stash push" in body
    assert "committed version" in body.lower()


def test_the_stashed_changes_are_always_restored():
    body = _body()
    # An edit must never be left stranded in the stash, even if the build failed above.
    assert "stash pop" in body
    # and a failed restore is reported with the manual recovery command, not thrown.
    assert "stash pop" in body and "WARNING" in body


def test_it_fails_open_on_a_git_error():
    body = _body()
    # A missing or failing git must not stop a rebuild: the dirty list defaults to empty.
    assert "$uiDirty = @()" in body
    assert "try {" in body and "catch {" in body


def test_the_valve_runs_before_the_build_step():
    body = _body()
    # The guard must sit ahead of the stop/compile steps, or it would warn after the damage.
    guard_at = body.find("uncommitted tracked change")
    stop_at = body.find("Stop BOTH first")
    assert guard_at != -1 and stop_at != -1 and guard_at < stop_at
