# -*- coding: utf-8 -*-
"""The composer box must empty the MOMENT a fresh run is spawned, not when it is confirmed.

fix/submit-box-clear-immediately. Before this change the cockpit held the operator's typed
goals until WatchFreshFleetLaunch saw a matching worker in status.json. A launch that was slow,
that failed, or whose text did not byte-match the worker goal left the box full and
_fleetLaunchPending raised, so the next submit was refused and the screen looked stuck.

These are source-level checks (the cockpit is a WPF exe; the C# is validated by string, the same
way the sibling submit tests are). They pin:
  * the box is cleared inside SpawnFleet on spawn success, guarded to the exact spawned text;
  * the accept branch of WatchFreshFleetLaunch no longer clears the box;
  * a bounded confirm window brings _fleetLaunchPending back down if the run is never confirmed;
  * failure, early-exit and timeout paths restore the operator's text (without clobbering newly
    typed input) via SubmitRestoreBox;
  * the spawn/accept/fail transitions are traced to .fleet/submit_timing.jsonl the no-BOM way.
"""
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import cockpit_source  # the cockpit source is split across ui/FleetCockpit*.cs

SRC = cockpit_source.read(encoding="utf-8-sig")


def _block(start, end):
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


def test_box_is_cleared_inside_spawn_on_success_guarded_to_the_spawned_text():
    spawn = _block("bool SpawnFleet(List<string> goals", "static bool FreshRunContainsGoals(")
    # Only the fresh-start path (waitForClosingRun) clears; and only when the box still holds
    # exactly what we spawned, so a retry/bulk-retry that did not fill the box cannot blank it.
    assert 'if (waitForClosingRun)' in spawn
    assert 'if (_goalInput != null && _goalInput.Text == (submittedText ?? ""))' in spawn
    assert '_goalInput.Text = "";' in spawn
    assert 'SubmitDbg("box_cleared_at_spawn")' in spawn
    # the row is added once (by NoteSubmitted); the clear must not add it again
    assert spawn.count('NoteSubmitted(goals, submitBaseline)') == 1
    # pending is armed with a timestamp so it can be timed out
    assert '_fleetLaunchPending = true;' in spawn
    assert '_fleetLaunchPendingSinceUtc = DateTime.UtcNow;' in spawn


def test_accept_branch_no_longer_clears_the_box():
    watch = _block("void WatchFreshFleetLaunch(", "// Render a goals list")
    # the confirmation path only updates status now
    assert 'if (_goalInput.Text == submittedText) _goalInput.Text = "";' not in watch
    assert 'SubmitDbg("accepted goals="' in watch


def test_pending_flag_is_bounded_by_a_confirm_window():
    watch = _block("void WatchFreshFleetLaunch(", "// Render a goals list")
    assert '_freshLaunchConfirmWindow' in watch
    assert 'SubmitDbg("timeout_unconfirmed")' in watch
    # every exit from the watch drops the flag
    assert watch.count('_fleetLaunchPending = false;') >= 3
    assert 'static readonly TimeSpan _freshLaunchConfirmWindow' in SRC


def test_failure_paths_restore_the_operator_text():
    watch = _block("void WatchFreshFleetLaunch(", "// Render a goals list")
    # INTEGRATION NOTE (fix/fleet-intake-never-lost): the ui_submit diff asserted == 3 here
    # (timeout, early-exit, exception). Merging the gui_start diff added a fourth restore on the
    # TIMED-OUT retry path (SpawnFleetRetry failed to relaunch), and SpawnFleetRetry now sits
    # between WatchFreshFleetLaunch and "// Render a goals list", so this block spans both. All
    # four are legitimate failure exits that must restore the operator's text; the count is 4.
    assert watch.count('SubmitRestoreBox(submittedText)') == 4
    # restore must not clobber text typed after submit: it appends instead of overwriting
    restore = _block("void SubmitRestoreBox(string submittedText)", "void NoteSubmitted(IEnumerable<string> goals)")
    assert 'cur.Trim() == submittedText.Trim()' in restore
    assert r'cur.TrimEnd() + "\n" + submittedText' in restore


def test_submit_timing_is_traced_without_a_byte_order_mark():
    dbg = _block("void SubmitDbg(string what)", "void SubmitRestoreBox(string submittedText)")
    assert 'submit_timing.jsonl' in dbg
    assert 'NoBomUtf8' in dbg
    assert 'File.AppendAllText(' in dbg
    # the click itself is traced
    assert 'SubmitDbg("start_click")' in SRC
    assert 'SubmitDbg("spawn_ok waitForClosingRun="' in SRC
