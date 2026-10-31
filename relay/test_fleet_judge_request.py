import json

from relay import judge_gate
from relay.relay_fleet import RelayWorker


GOAL = "odbc_query で検査件数を集計して表にする"


def worker(folder):
    w = RelayWorker.__new__(RelayWorker)
    w.goal, w.cwd = GOAL, str(folder)
    w.last_response, w.reason = "answer", ""
    w._record_tree_stability = lambda: None
    w._claim_verdict = lambda: "DONE"
    return w


def test_present_request_calls_judge_and_completes(tmp_path, monkeypatch):
    data = str(tmp_path / "data.csv")
    request = {"claim_id": "SYN1", "data_location": data,
               "fetched_at": "2026-10-30T00:00:00+09:00"}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    calls = []
    def judge(claim_id, **kwargs):
        calls.append((claim_id, kwargs))
        return {"engine_available": True, "id": claim_id, "verdict": "HOLD"}
    monkeypatch.setattr(judge_gate, "run_engine_judge", judge)
    w = worker(tmp_path)
    w._settle_done()
    assert calls == [("SYN1", {"data_location": data, "fetched_at": request["fetched_at"]})]
    assert w.status == "done" and w.outcome == "DONE"
    assert "HOLD" in w.last_response


def test_absent_request_completes_with_visible_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_JUDGE_STRICT", raising=False)
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "done" and w.outcome == "DONE"
    assert "judge: none" in w.reason and "judge: none" in w.last_response


def test_strict_missing_request_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_JUDGE_STRICT", "1")
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "stuck" and w.outcome == "VERIFY_FAILED"
    assert "judge: none" in w.reason


def test_engine_refuses_a_manifest_with_a_different_fetch_time(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "configs" / "SYN1.json").write_text(
        json.dumps({"dataset_id": "synthetic"}), encoding="utf-8")
    (tmp_path / "data" / "manifest.json").write_text(json.dumps({"datasets": {
        "synthetic": {"path": "data/input.csv", "fetched_at": "old"}}}), encoding="utf-8")
    verdict = judge_gate.run_engine_judge("SYN1", root=str(tmp_path),
                                         data_location=str(tmp_path / "data" / "input.csv"),
                                         fetched_at="new")
    assert not verdict["engine_available"]
    assert "fetch time" in verdict["unavailable_reason"]
