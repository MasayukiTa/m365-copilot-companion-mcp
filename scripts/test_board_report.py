#!/usr/bin/env python3
"""Tests for scripts/board_report.py (synthetic data only; read-only)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import board_report  # noqa: E402


def _write(tmp_path, name, rows):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _board_result(cid, idx):
    return {
        "v": 1,
        "kind": "result",
        "author_role": "coordinator",
        "campaign_id": cid,
        "ref": "task %s-%d" % (cid, idx),
    }


def _finding(cid, idx, findings, hchars, rchars):
    return {
        "v": 1,
        "campaign_id": cid,
        "subtask_index": idx,
        "findings": findings,
        "handoff_chars": hchars,
        "result_chars": rchars,
    }


def _make_fleet(tmp_path, n_completed, n_useful, n_empty, extra_board=None):
    board = []
    finds = []
    for i in range(n_completed):
        board.append(_board_result("c%02d" % i, 1))
    for i in range(n_useful):
        finds.append(_finding("c%02d" % i, 1, "[{'kind': 'fact'}]", 500, 1000))
    for i in range(n_empty):
        finds.append(_finding("e%02d" % i, 1, "[]", 100, 200))
    for row in extra_board or []:
        board.append(row)
    _write(tmp_path, "board.jsonl", board)
    _write(tmp_path, "relay_findings_shadow.jsonl", finds)
    return str(tmp_path)


def test_insufficient_below_min_completions(tmp_path):
    d = _make_fleet(tmp_path, n_completed=5, n_useful=5, n_empty=0)
    rep = board_report.build_report(d, min_completions=20)
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["completed_subtasks"] == 5


def test_insufficient_when_no_handoffs(tmp_path):
    d = _make_fleet(tmp_path, n_completed=30, n_useful=0, n_empty=0)
    rep = board_report.build_report(d, min_completions=20)
    assert rep["handoffs"] == 0
    assert rep["verdict"] == "INSUFFICIENT"


def test_no_worker_traffic_when_only_coordinator_rows(tmp_path):
    # Enough completions + hand-offs, but board carries no post/read/deny audit rows.
    d = _make_fleet(tmp_path, n_completed=30, n_useful=30, n_empty=0)
    rep = board_report.build_report(d, min_completions=20)
    assert rep["audit_rows"] == 0
    assert rep["M4_misinformation_ratio"] is None
    assert rep["verdict"] == "NO_WORKER_TRAFFIC"


def test_communication_pays_above_threshold(tmp_path):
    extra = [
        {"v": 1, "kind": "post", "campaign_id": "c00", "ref": "task c00-1"},
        {"v": 1, "kind": "read", "campaign_id": "c01", "ref": "task c01-1"},
    ]
    d = _make_fleet(tmp_path, n_completed=30, n_useful=24, n_empty=6, extra_board=extra)
    rep = board_report.build_report(d, min_completions=20, useful_threshold=0.5)
    assert rep["handoffs"] == 30
    assert rep["useful_handoffs"] == 24
    assert abs(rep["M2_useful_ratio"] - 0.8) < 1e-9
    assert rep["audit_rows"] == 2
    assert rep["verdict"] == "COMMUNICATION_PAYS"


def test_communication_thin_below_threshold(tmp_path):
    extra = [
        {"v": 1, "kind": "post", "campaign_id": "c00", "ref": "task c00-1"},
        {"v": 1, "kind": "deny", "campaign_id": "c01", "ref": "task c01-1"},
    ]
    d = _make_fleet(tmp_path, n_completed=30, n_useful=6, n_empty=24, extra_board=extra)
    rep = board_report.build_report(d, min_completions=20, useful_threshold=0.5)
    assert abs(rep["M2_useful_ratio"] - 0.2) < 1e-9
    assert rep["deny_rows"] == 1
    assert rep["audit_rows"] == 2
    assert abs(rep["M4_misinformation_ratio"] - 0.5) < 1e-9
    assert rep["verdict"] == "COMMUNICATION_THIN"


def test_comm_per_completion_counts_all_board_rows(tmp_path):
    extra = [{"v": 1, "kind": "post", "campaign_id": "c00", "ref": "task c00-1"}]
    d = _make_fleet(tmp_path, n_completed=20, n_useful=20, n_empty=0, extra_board=extra)
    rep = board_report.build_report(d, min_completions=20)
    # 20 result rows + 1 post row = 21 board rows / 20 completed.
    assert rep["board_rows"] == 21
    assert rep["completed_subtasks"] == 20
    assert abs(rep["M1_comm_per_completion"] - 21.0 / 20.0) < 1e-9


def test_context_growth_distribution(tmp_path):
    d = _make_fleet(tmp_path, n_completed=25, n_useful=25, n_empty=0)
    rep = board_report.build_report(d, min_completions=20)
    # all hand-offs have handoff_chars=500, result_chars=1000
    assert rep["M3_handoff_chars"]["n"] == 25
    assert rep["M3_handoff_chars"]["median"] == 500
    assert rep["M3_handoff_chars"]["max"] == 500
    assert rep["M3_result_chars"]["median"] == 1000


def test_missing_files_are_empty_not_error(tmp_path):
    # No board.jsonl / findings file at all -> empty, INSUFFICIENT, no exception.
    rep = board_report.build_report(str(tmp_path), min_completions=20)
    assert rep["board_rows"] == 0
    assert rep["handoffs"] == 0
    assert rep["verdict"] == "INSUFFICIENT"


def test_malformed_lines_are_skipped(tmp_path):
    bp = os.path.join(str(tmp_path), "board.jsonl")
    with open(bp, "w", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(_board_result("c00", 1)) + "\n")
        fh.write("\n")
    fp = os.path.join(str(tmp_path), "relay_findings_shadow.jsonl")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write("garbage\n")
    rep = board_report.build_report(str(tmp_path), min_completions=1)
    assert rep["board_rows"] == 1
    assert rep["completed_subtasks"] == 1


def test_render_text_runs(tmp_path):
    d = _make_fleet(tmp_path, n_completed=30, n_useful=30, n_empty=0)
    rep = board_report.build_report(d, min_completions=20)
    text = board_report.render_text(rep)
    assert "Board communication telemetry" in text
    assert "verdict:" in text


def test_main_json_smoke(tmp_path, capsys):
    d = _make_fleet(tmp_path, n_completed=30, n_useful=30, n_empty=0)
    rc = board_report.main(["--fleet-dir", d, "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["verdict"] == "NO_WORKER_TRAFFIC"
