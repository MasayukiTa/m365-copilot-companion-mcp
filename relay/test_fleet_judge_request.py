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
    assert "manifest: not run:" in w.last_response
    assert "registration: not run:" in w.last_response
    assert "lot diagnosis: not run:" in w.last_response


def test_complete_request_calls_harness_with_supplied_values(tmp_path, monkeypatch):
    from tools import validity_harness as vh
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
               "registration": {"root": "reg", "claim": {"id": "SYN1", "test": 1},
                                "registered_at": "2026-10-29T00:00:00+09:00"},
               "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    calls = []
    monkeypatch.setattr(vh, "record_manifest", lambda *a, **kw: calls.append(("manifest", a, kw)))
    def register(*a, **kw):
        calls.append(("registration", a, kw))
        return {"order_ok": True}
    monkeypatch.setattr(vh, "register_and_lock", register)
    def diagnose(**kw):
        calls.append(("lot", (), kw))
        return {"checked": True}
    monkeypatch.setattr(vh, "lot_independence_check", diagnose)
    monkeypatch.setattr(judge_gate, "run_engine_judge", lambda *a, **kw: {"engine_available": False})
    ok, status = judge_gate.complete_judge_request(GOAL, str(tmp_path))
    assert ok and "not run:" not in status
    assert calls == [
        ("manifest", (str(tmp_path / "manifest.json"),),
         {"dataset_id": "ds", "data_path": str(tmp_path / "data.csv"),
          "result": {"n": 2}, "fetched_at": request["fetched_at"]}),
        ("registration", (str(tmp_path / "reg"),),
         {"claim": request["registration"]["claim"], "data_fetched_at": request["fetched_at"],
          "registered_at": request["registration"]["registered_at"]}),
        ("lot", (), request["lot_diagnosis"])]


def test_partial_request_runs_only_complete_section(tmp_path, monkeypatch):
    from tools import validity_harness as vh
    request = {"claim_id": "SYN1", "data_location": "data.csv", "fetched_at": "today",
               "manifest": {"path": "m.json", "dataset_id": "ds", "result": {}},
               "registration": {"root": "reg"}, "lot_diagnosis": {"y": [1]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    calls = []
    monkeypatch.setattr(vh, "record_manifest", lambda *a, **kw: calls.append("manifest"))
    monkeypatch.setattr(vh, "register_and_lock", lambda *a, **kw: calls.append("registration"))
    monkeypatch.setattr(vh, "lot_independence_check", lambda **kw: calls.append("lot"))
    monkeypatch.setattr(judge_gate, "run_engine_judge", lambda *a, **kw: {"engine_available": False})
    ok, status = judge_gate.complete_judge_request(GOAL, str(tmp_path))
    assert ok and calls == ["manifest"]
    assert "registration: not run: root, claim and registered_at are required" in status
    assert "lot diagnosis: not run: y, x and lot are required" in status


def test_complete_request_runs_real_harness(tmp_path, monkeypatch):
    (tmp_path / "data.csv").write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
               "registration": {"root": "reg", "claim": {"id": "SYN1"},
                                "registered_at": "2026-10-29T00:00:00+09:00"},
               "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(judge_gate, "run_engine_judge", lambda *a, **kw: {"engine_available": False})
    ok, status = judge_gate.complete_judge_request(GOAL, str(tmp_path))
    assert ok and "manifest: recorded" in status and "order_ok=True" in status
    assert '"checked": true' in status and '"independent": false' in status
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["datasets"]["ds"]
    assert json.loads((tmp_path / "reg" / "registry" / "SYN1.json").read_text(encoding="utf-8")) == {"id": "SYN1"}


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
