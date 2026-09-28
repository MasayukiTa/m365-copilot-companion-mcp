# -*- coding: utf-8 -*-
"""A genuinely unstarted task older than the pickup budget must become observable.

The health dots keep their own semantics: a working server/tool path stays green. Queue starvation
is published as a separate queue-health fact and a top-strip warning, so an old submitted row is
not something an operator has to discover by scrolling.
"""
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
  static int Main() {
    var v = new List<SubmittedView>();
    v.Add(new SubmittedView { Source="taken", AgeS=3900, Stale=true, Goal="old consumed task" });
    v.Add(new SubmittedView { Source="pending", AgeS=800, Stale=true, Goal="old queued task" });
    v.Add(new SubmittedView { Source="command", AgeS=20, Stale=false, Goal="fresh task" });
    SubmittedHealth h = SubmittedTasks.SummarizeHealth(v);
    if (h.Total != 3) return 10;
    if (h.StaleCount != 2) return 11;
    if (h.TakenStaleCount != 1) return 12;
    if (Math.Abs(h.OldestAgeS - 3900) > 0.1) return 13;
    return 0;
  }
}
'''


def test_queue_health_summary_counts_real_stale_and_taken_stale(tmp_path):
    h = tmp_path / "H.cs"
    h.write_text(HARNESS, encoding="utf-8")
    exe = tmp_path / "H.exe"
    r = subprocess.run([str(CSC), "/nologo", "/target:exe", "/out:" + str(exe),
                        "/r:" + str(FW / "System.Web.Extensions.dll"),
                        str(UI / "SubmittedTasks.cs"), str(h)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and exe.is_file(), (r.stdout, r.stderr)
    q = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert q.returncode == 0, (q.returncode, q.stdout, q.stderr)


def test_published_health_strip_contains_stale_queue_metrics():
    s = (UI / "FleetCockpit.cs").read_text(encoding="utf-8-sig")
    i = s.index("void PublishHealthStrip()")
    b = s[i:i + 6500]
    assert "SubmittedTasks.SummarizeHealth(qj)" in b
    assert "queued_stale_count" in b
    assert "queued_taken_stale_count" in b
    assert "queued_oldest_age_s" in b


def test_top_health_strip_warns_about_stale_submitted_work_without_recoloring_a_dot():
    s = (UI / "FleetCockpit.cs").read_text(encoding="utf-8-sig")
    i = s.index("void ApplyHealthToUi()")
    b = s[i:i + 6000]
    assert "SubmittedTasks.SummarizeHealth(ReadQueuedJobs())" in b
    assert 'T("hs_queue_stale")' in b
    assert "Theme.Warning(_dark)" in b
    assert "SetDot(" not in b[b.index('T("hs_queue_stale")') - 500:b.index('T("hs_queue_stale")') + 500]
