# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"
FW = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319")
CSC = FW / "csc.exe"

pytestmark = pytest.mark.skipif(os.name != "nt" or not CSC.is_file(), reason="requires Windows csc")

HARNESS = r'''
using System;
using System.Collections.Generic;
class H {
  static Dictionary<string,object> Row(string goal, string key) {
    var d = new Dictionary<string,object>(); d["goal"] = goal; d["key"] = key;
    d["name"] = key; d["status"] = "done"; d["outcome"] = "DONE"; return d;
  }
  static int Main() {
    string g = "same goal retried later";
    var workers = new List<Dictionary<string,object>>();
    var oldHist = new List<Dictionary<string,object>>(); oldHist.Add(Row(g, "old#w3"));
    var newerHist = new List<Dictionary<string,object>>(); newerHist.Add(Row(g, "old#w3")); newerHist.Add(Row(g, "new#w9"));

    // A retry must not be swallowed by an old history row that predates the submission.
    var st = new SubmittedTasks();
    if (!st.AddLocal(g, 1000, "newrun", workers, oldHist)) return 10;
    if (st.Refresh(new List<SubmittedFile>(), "newrun", workers, oldHist, 1001).Count != 1) return 11;
    if (st.Refresh(new List<SubmittedFile>(), "newrun", workers, newerHist, 1002).Count != 0) return 12;

    // A command that was visibly backed, then consumed, becomes `taken`; history from the
    // worker that actually consumed it must clear the false "not picked up" row even if a
    // later run has replaced status.json.
    var st2 = new SubmittedTasks();
    var files = new List<SubmittedFile>(); files.Add(new SubmittedFile { Source="command", Id="c1", Goal=g, SubmittedUnix=2000 });
    if (st2.Refresh(files, "run2", workers, oldHist, 2001).Count != 1) return 20;
    var taken = st2.Refresh(new List<SubmittedFile>(), "run2", workers, oldHist, 2002);
    if (taken.Count != 1 || taken[0].Source != "taken") return 21;
    if (st2.Refresh(new List<SubmittedFile>(), "run2", workers, newerHist, 2003).Count != 0) return 22;
    return 0;
  }
}
'''


def test_submitted_rows_reconcile_against_new_history_without_swallowing_retries(tmp_path):
    h = tmp_path / "H.cs"
    h.write_text(HARNESS, encoding="utf-8")
    exe = tmp_path / "H.exe"
    r = subprocess.run([str(CSC), "/nologo", "/target:exe", "/out:" + str(exe),
                        "/r:" + str(FW / "System.Web.Extensions.dll"),
                        str(UI / "SubmittedTasks.cs"), str(h)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and exe.is_file(), (r.stdout, r.stderr)
    q = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert q.returncode == 0, (q.returncode, q.stdout, q.stderr)


def test_cockpit_passes_history_to_both_submission_merge_boundaries():
    src = (UI / "FleetCockpit.cs").read_text(encoding="utf-8-sig")
    refresh = src[src.index("void RefreshSubmitted("):src.index("static string StartedOf", src.index("void RefreshSubmitted("))]
    capture = src[src.index("SubmissionBaseline CaptureSubmissionBaseline()"):
                  src.index("void NoteSubmitted(IEnumerable<string> goals)")]
    note = src[src.index("void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)"):
               src.index("static string OneLine", src.index("void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)"))]
    assert "HistoryWorkers()" in refresh
    assert "HistoryWorkers()" in capture
    assert "WorkersOf(root)" in capture
    assert "_submitted.Refresh(files, StartedOf(root)," in refresh
    assert "_submitted.AddLocal(" in note
    assert "b.Started, b.Workers, b.History" in note


def _method_block(src, name, next_marker):
    i = src.index(name)
    j = src.index(next_marker, i)
    return src[i:j]


def test_submission_baseline_is_captured_before_handoff_can_create_a_worker():
    src = (UI / "FleetCockpit.cs").read_text(encoding="utf-8-sig")
    assert "sealed class SubmissionBaseline" in src
    assert "SubmissionBaseline CaptureSubmissionBaseline()" in src
    assert "void NoteSubmitted(IEnumerable<string> goals, SubmissionBaseline baseline)" in src

    live = _method_block(src, "void TryAddGoalsToLiveFleet()", "void WatchLiveAddHandoff(")
    assert live.index("CaptureSubmissionBaseline()") < live.index("SendTrackedCommand(")
    assert "NoteSubmitted(goals, submitBaseline)" in live

    spawn = _method_block(src, "bool SpawnFleet(List<string> goals", "string GoalsToJsonl(")
    assert spawn.index("CaptureSubmissionBaseline()") < spawn.index("Process.Start(psi)")
    assert "NoteSubmitted(goals, submitBaseline)" in spawn

    durable = _method_block(src, "bool SpawnDurableTask(string goal)", "bool SpawnFleet(List<string> goals")
    assert durable.index("CaptureSubmissionBaseline()") < durable.index("Process.Start(psi)")
    assert "NoteSubmitted(new List<string> { goal }, submitBaseline)" in durable

    retry = _method_block(src, "void RetryGoal(Dictionary<string, object> w)", "Dictionary<string, object> Cmd1")
    live_branch = retry[:retry.index("string goal = S(w, \"goal\")")]
    assert live_branch.index("CaptureSubmissionBaseline()") < live_branch.index("SendCommand(")
    assert "NoteSubmitted(new List<string> { S(w, \"goal\") }, submitBaseline)" in live_branch
