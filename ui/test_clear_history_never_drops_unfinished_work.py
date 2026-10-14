# -*- coding: utf-8 -*-
"""Clear History / bulk resume / run counts must not lose or misreport unfinished work.

Source-level checks (the cockpit is a WPF exe; these pin the decisions that failed in production):
 * ClearHistory never rewrites status.json and never hides non-terminal workers. It used to infer
   "run is dead" from status.json's `updated` age and blank the snapshot, so a wedged-but-alive
   runner's ready/waiting/running workers vanished.
 * Liveness for destructive actions comes from the runner process (pid + pid_birth).
 * The bulk-resume button gives immediate feedback and delivers off the UI thread.
 * The header counts are per state; `ready` is not "running".
"""
from pathlib import Path

SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")
SUBMITTED = Path(__file__).with_name("SubmittedTasks.cs").read_text(encoding="utf-8")


def _block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_clear_history_never_blanks_status_or_hides_unfinished_workers():
    body = _block(SOURCE, "void ClearHistory()", "void ArchiveRunTailOnce(")
    assert "WriteAllText(_statusPath" not in body
    assert "_hiddenKeys.Clear()" not in body
    assert "IsTerminalWorker(w)" in body
    assert "idle" not in body
    # no liveness guess from the snapshot's age in this method
    assert "NowUnix() - updated" not in body


def test_runner_liveness_uses_the_process_not_the_snapshot_age():
    fn = _block(SOURCE, "bool RunnerProcessAlive()", "bool FleetRunIsLive()")
    assert "fleet_run_active.json" in fn
    assert "pid_birth" in fn
    assert "StartTime" in fn
    assert "GetProcessById" in fn


def test_stop_all_does_not_retire_a_live_runners_workers_on_age_alone():
    i = SOURCE.index("ArchiveAllStale();")
    window = SOURCE[i - 700:i]
    assert "!RunnerProcessAlive()" in window


def test_bulk_resume_acknowledges_at_once_and_delivers_off_the_ui_thread():
    i = SOURCE.index("retryAll.Click += delegate")
    handler = SOURCE[i:i + 2500]
    assert "retryAll.IsEnabled = false" in handler
    assert "再開中…" in handler
    body = _block(SOURCE, "int RetryAllShown(", "static readonly string[] _retryableOutcomes")
    assert "ThreadPool.QueueUserWorkItem" in body
    assert "Dispatcher.BeginInvoke" in body


def test_submission_index_is_built_once_per_batch():
    note = _block(SOURCE, "void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)",
                  "static string OneLine")
    assert note.count("BuildBaselineIndex(") == 1
    assert "public bool AddLocal(string goal, double nowUnix, Dictionary<string, List<string>> byGoal)" in SUBMITTED


def test_header_counts_ready_separately_from_running():
    assert 'else if (wst == "ready") cntReady++;' in SOURCE
    assert '" 準備"' in SOURCE
    assert '" 待機列 ' in SOURCE
