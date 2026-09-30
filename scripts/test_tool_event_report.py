# -*- coding: utf-8 -*-
"""scripts/tool_event_report.py: counts, percentiles, gaps, fill rates; read-only."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import tool_event_report as R  # noqa: E402


def _pair(i, tool, start, dur, proc="p", session="s", task="", worker="", attr=None, ok=True):
    call = {"event": "call", "id": str(i), "ts": start, "mono": start, "proc": proc,
            "tool": tool, "task": task, "worker": worker, "session": session}
    if attr:
        call["attr"] = attr
    out = {"event": "outcome", "id": str(i), "ts": start + dur, "mono": start + dur, "proc": proc,
           "ok": ok, "dur_mono_s": dur, "duration_s": dur}
    return [call, out]


def test_percentile_is_nearest_rank_and_empty_safe():
    assert R.percentile([], 50) is None
    assert R.percentile([1, 2, 3, 4], 50) == 2
    assert R.percentile([1, 2, 3, 4], 95) == 4


def test_report_counts_durations_gaps_and_fill_rates():
    rows = []
    rows += _pair(1, "read_file", 100.0, 1.0, task="j1", worker="w1", attr="session")
    rows += _pair(2, "call_tool.catalogue", 103.0, 0.5)
    rows += _pair(3, "read_file", 110.0, 2.0, task="j1", worker="w1", attr="session", ok=False)
    rows.append({"event": "call", "id": "9", "ts": 200.0, "tool": "shell", "session": "s"})
    a = R.analyse(rows)
    assert a["calls"] == 4 and a["orphans"] == 1 and a["discovery"] == 1
    assert a["task_filled"] == 2 and a["worker_filled"] == 2 and a["session_attributed"] == 2
    assert sorted(a["gaps"]) == [2.0, 6.5]
    assert R.percentile([d for d in a["per_tool"]["read_file"] if d], 50) == 1.0
    assert a["per_task"]["j1"]["failed"] == 1


def test_gap_falls_back_to_wall_clock_across_processes():
    rows = _pair(1, "a", 100.0, 1.0, proc="p1") + _pair(2, "b", 105.0, 1.0, proc="p2")
    rows[2]["mono"] = 5.0            # a different process's monotonic clock is not comparable
    assert R.analyse(rows)["gaps"] == [4.0]


def test_render_is_markdown_and_main_reads_without_writing(tmp_path, capsys):
    p = tmp_path / "tool_events.jsonl"
    body = "".join(json.dumps(r) + "\n" for r in _pair(1, "read_file", 1.0, 0.25))
    p.write_text(body, encoding="utf-8")
    assert R.main(["--path", str(p)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("# Tool event report") and "| read_file | 1 | 1 |" in text
    assert p.read_text(encoding="utf-8") == body


def test_missing_ledger_reports_zero_not_a_crash(tmp_path, capsys):
    assert R.main(["--path", str(tmp_path / "none.jsonl")]) == 0
    assert "calls: 0" in capsys.readouterr().out
