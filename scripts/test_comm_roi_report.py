# -*- coding: utf-8 -*-
"""Deterministic tests for scripts/comm_roi_report.py on synthetic fleet records.

No live data, no network, no OS-specific paths: every input is built in a tmp dir with forward
slashes via os.path.join, so the suite runs the same on Linux CI and on Windows.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import comm_roi_report as R  # noqa: E402


def _edge(cid, idx, author_role="coordinator"):
    return {"v": 1, "kind": "result", "author": author_role, "author_role": author_role,
            "campaign_id": cid, "subject": "subtask %d" % idx, "ref": "task %s-%d" % (cid, idx),
            "ts": 1000 + idx, "expires_ts": 9999}


def _handoff(cid, idx, kinds):
    return {"v": 1, "mode": "shadow", "campaign_id": cid, "subtask_index": idx,
            "kinds": dict(kinds), "findings": [{"kind": k, "line": "x"} for k in kinds
                                               for _ in range(kinds[k])],
            "result_chars": 100, "handoff_chars": (10 if kinds else 0)}


def _write(tmp, board, findings):
    os.makedirs(tmp, exist_ok=True)
    with io.open(os.path.join(tmp, "board.jsonl"), "w", encoding="utf-8") as fh:
        for r in board:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with io.open(os.path.join(tmp, "relay_findings_shadow.jsonl"), "w", encoding="utf-8") as fh:
        for r in findings:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- pure helpers

def test_edge_index_from_subject_and_ref():
    assert R._edge_index({"subject": "subtask 3", "ref": ""}) == 3
    assert R._edge_index({"subject": "", "ref": "task cabc-7"}) == 7
    assert R._edge_index({"subject": "no index here", "ref": ""}) is None


def test_finding_count_prefers_kinds():
    assert R._finding_count({"kinds": {"path": 2, "fact": 3}}) == 5
    assert R._finding_count({"kinds": {}, "findings": [1, 2]}) == 2
    assert R._finding_count({}) == 0


def test_is_edge_requires_result_kind_and_coordinator():
    assert R._is_edge({"kind": "result", "author_role": "coordinator"})
    assert not R._is_edge({"kind": "fact", "author_role": "coordinator"})
    assert not R._is_edge({"kind": "result", "author_role": "worker"})


# ---------------------------------------------------------------- analyse / join

def test_carrying_and_wasteful_edges_counted():
    cid = "c1"
    board = [_edge(cid, 1), _edge(cid, 2), _edge(cid, 3)]
    # idx1 carries 4 findings, idx2 carries 0 (empty hand-off), idx3 has no hand-off row at all.
    findings = [_handoff(cid, 1, {"path": 1, "fact": 3}), _handoff(cid, 2, {})]
    res = R.analyse(board, findings)
    row = [r for r in res["rows"] if r["campaign"] == cid][0]
    assert row["edges"] == 3
    assert row["carrying"] == 1
    assert row["empty"] == 1       # idx2 matched but carried nothing
    assert row["unmatched"] == 1   # idx3 had no hand-off row
    assert row["wasteful"] == 2
    assert row["findings_carried"] == 4
    assert abs(row["roi"] - 4 / 3) < 1e-9


def test_qualifies_needs_two_edges_and_a_handoff():
    # one edge, one hand-off -> too few edges -> not qualifying
    res = R.analyse([_edge("c9", 1)], [_handoff("c9", 1, {"fact": 1})])
    assert res["rows"][0]["qualifies"] is False
    # two edges but NO hand-off row anywhere for the campaign -> not qualifying
    res2 = R.analyse([_edge("c8", 1), _edge("c8", 2)], [])
    assert res2["rows"][0]["qualifies"] is False
    # two edges and a hand-off -> qualifies
    res3 = R.analyse([_edge("c7", 1), _edge("c7", 2)], [_handoff("c7", 1, {"fact": 1})])
    assert res3["rows"][0]["qualifies"] is True


def test_richest_handoff_wins_on_resettle():
    # a re-settled slice: empty record then a rich one; the rich one must win.
    cid = "c2"
    board = [_edge(cid, 1), _edge(cid, 2)]
    findings = [_handoff(cid, 1, {}), _handoff(cid, 1, {"gap": 2})]
    res = R.analyse(board, findings)
    row = res["rows"][0]
    assert row["carrying"] == 1
    assert row["findings_carried"] == 2


def test_non_edge_rows_are_not_edges():
    board = [_edge("c3", 1), {"kind": "fact", "author_role": "worker", "campaign_id": "c3"}]
    res = R.analyse(board, [_handoff("c3", 1, {"fact": 1})])
    assert res["buckets"]["edges"] == 1
    assert res["buckets"]["non_edge_rows"] == 1


# ---------------------------------------------------------------- verdict

def test_verdict_insufficient_when_no_edges():
    s = R.summarise(R.analyse([], []))
    assert R.verdict(s).startswith("INSUFFICIENT")


def test_verdict_insufficient_when_too_few_campaigns():
    board = [_edge("only", 1), _edge("only", 2)]
    s = R.summarise(R.analyse(board, [_handoff("only", 1, {"fact": 1})]))
    assert s["qualifying_campaigns"] == 1
    assert R.verdict(s).startswith("INSUFFICIENT")


def _many_campaigns(n, wasteful_per):
    """n campaigns, each with 4 edges; `wasteful_per` of them carry nothing."""
    board, findings = [], []
    for i in range(n):
        cid = "c%03d" % i
        for idx in range(1, 5):
            board.append(_edge(cid, idx))
            if idx <= (4 - wasteful_per):
                findings.append(_handoff(cid, idx, {"fact": 2}))
            else:
                findings.append(_handoff(cid, idx, {}))  # empty -> wasteful
    return board, findings


def test_verdict_edges_earn_cost_when_few_wasteful():
    board, findings = _many_campaigns(10, wasteful_per=0)
    s = R.summarise(R.analyse(board, findings))
    assert s["qualifying_campaigns"] == 10
    assert s["wasteful_share"] == 0.0
    assert R.verdict(s) == "EDGES_EARN_COST"


def test_verdict_prune_candidates_when_many_wasteful():
    board, findings = _many_campaigns(10, wasteful_per=2)  # 2/4 = 50% wasteful > 25%
    s = R.summarise(R.analyse(board, findings))
    assert s["wasteful_share"] == 0.5
    assert R.verdict(s) == "PRUNE_CANDIDATES"


# ---------------------------------------------------------------- end to end (reads a tmp dir)

def test_main_text_and_json_on_tmp_fleet(tmp_path, capsys):
    tmp = os.path.join(str(tmp_path), ".fleet")
    board, findings = _many_campaigns(9, wasteful_per=1)
    _write(tmp, board, findings)

    assert R.main(["--fleet-dir", tmp, "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "Communication-ROI report" in text
    assert "R4 communication ROI" in text

    assert R.main(["--fleet-dir", tmp, "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"]["qualifying_campaigns"] == 9
    assert doc["verdict"] in ("EDGES_EARN_COST", "PRUNE_CANDIDATES")
    # 1 of 4 edges wasteful per campaign == 25%, not strictly greater than the 25% limit.
    assert abs(doc["summary"]["wasteful_share"] - 0.25) < 1e-9
    assert doc["verdict"] == "EDGES_EARN_COST"


def test_main_handles_missing_fleet_dir(capsys):
    assert R.main(["--fleet-dir", os.path.join("nowhere", "nope"), "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"].startswith("INSUFFICIENT")
