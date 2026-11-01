import json
from types import SimpleNamespace

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
          "registered_at": request["registration"]["registered_at"],
          "data_location": str(tmp_path / "data.csv")}),
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
    import os as _os
    import time as _time
    data = tmp_path / "data.csv"
    data.write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    # The data file provably existed by its mtime. Put that mtime in the FUTURE so the trusted
    # (now) registration precedes it -> order_ok True. This is the honest case: register, then
    # the data is fetched.
    future = _time.time() + 3600
    _os.utime(data, (future, future))
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
    # The trusted registration time was stamped by the harness, not taken from the worker's
    # declared 2026-10-29 value, which is kept only as declared.
    lock = json.loads((tmp_path / "reg" / "registry" / "lock_log.jsonl")
                      .read_text(encoding="utf-8").splitlines()[-1])
    assert lock["declared_registered_at"] == "2026-10-29T00:00:00+09:00"
    assert lock["registered_at"] != "2026-10-29T00:00:00+09:00"
    assert lock["registered_at_trusted"] is True and lock["order_ok"] is True
    assert lock["data_file_mtime"]  # order rests on file-mtime evidence, not a declared string
    # The authoritative record is written by the harness with the full footer, next to output.
    result = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert result["source"] == "harness"
    assert "判定器は実行されませんでした" in result["footer"]
    assert result["engine_status"].startswith("judge: not run (engine unavailable:")
    assert judge_gate.judge_result_pointer(str(tmp_path)).startswith(
        "judge: recorded -> judge_result.json |")


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


def test_resolved_case_request_classifies_short_reference(tmp_path):
    from tools.search_ops import case_reference_search
    folder = tmp_path / "枝さん_679かすれ調査"
    folder.mkdir()
    (folder / "やりたいこと.txt").write_text(
        "検査データの不良件数とロット別の設備条件を集計し相関を調べる。", encoding="utf-8")
    goal = "枝さん_679かすれ調査　これ調べて"
    result = case_reference_search(goal, roots=[tmp_path])
    request = next(c for c in result["candidates"] if c["kind"] == "directory")
    text = (folder / "やりたいこと.txt").read_text(encoding="utf-8")
    assert request["request"] == str(folder / "やりたいこと.txt")
    assert not judge_gate.is_data_analysis_goal(goal)
    assert judge_gate.is_data_analysis_goal(goal, text)
    assert judge_gate.complete_judge_request(goal, str(tmp_path), request_text=text) == (True, "judge: none")


def test_recorded_tools_at_completion_and_strict(tmp_path, monkeypatch):
    from relay import relay_fleet
    monkeypatch.delenv("MCP_JUDGE_STRICT", raising=False)
    w = worker(tmp_path)
    w.goal, w.name, w._first_turn_sent_at = "案件を調べて", "w0", 100
    w._conversation_ids_for_binding = lambda: {"client": "conv"}
    w._ledger_rows = lambda: [
        {"event": "call", "ts": 101, "session": "other", "tool": "odbc_query"},
        {"event": "call", "ts": 102, "session": "mine", "tool": "run_python"}]
    bindings = {"mine": {"worker": "w0", "conversation": "conv", "how": "id"},
                "other": {"worker": "w1", "conversation": "other", "how": "id"}}
    monkeypatch.setattr(relay_fleet, "_socket_route", lambda: SimpleNamespace(
        mcp_bindings=SimpleNamespace(snapshot=lambda: bindings)))
    assert w._judge_tool_names() == {"run_python"}
    w._settle_done()
    assert w.status == "done" and "judge: none" in w.last_response
    w._ledger_rows = lambda: [{"event": "call", "ts": 104, "session": "mine", "tool": "odbc_query"}]
    assert w._judge_tool_names() == {"odbc_query"}
    monkeypatch.setenv("MCP_JUDGE_STRICT", "1")
    w._settle_done()
    assert w.outcome == "VERIFY_FAILED" and "judge: none" in w.reason
    w._ledger_rows = lambda: [{"event": "call", "ts": 103, "session": "other", "tool": "odbc_query"}]
    monkeypatch.delenv("MCP_JUDGE_STRICT")
    w.last_response, w.reason = "answer", ""
    w._settle_done()
    assert w.status == "done" and w.last_response == "answer" and not w.reason


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


# --- trusted registration order (欠け 2026-11-01) --------------------------------------------

def _lock_rec(root):
    import pathlib
    line = pathlib.Path(root, "registry", "lock_log.jsonl").read_text(
        encoding="utf-8").splitlines()[-1]
    return json.loads(line)


def test_backdated_declared_registration_does_not_pass_the_order_check(tmp_path):
    """A worker backdates its registration to look pre-registered. The trusted time is stamped
    by the harness from the clock, the declared value is kept but NOT used for order_ok, and
    because the data file already exists when the trusted registration happens, order_ok=False."""
    from tools import validity_harness as vh
    import os as _os, time as _time
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n", encoding="utf-8")
    # Data already existed an hour AGO; a registration happening now is after it.
    past = _time.time() - 3600
    _os.utime(data, (past, past))
    rec = vh.register_and_lock(
        str(tmp_path / "reg"), claim={"id": "SYN1"},
        data_fetched_at="2026-10-30T00:00:00+09:00",
        registered_at="2000-01-01T00:00:00+09:00",  # backdated by the worker
        data_location=str(data))
    assert rec["declared_registered_at"] == "2000-01-01T00:00:00+09:00"
    assert rec["registered_at"] != "2000-01-01T00:00:00+09:00"  # trusted, from the clock
    assert rec["registered_at_trusted"] is True
    assert rec["order_ok"] is False  # registered AFTER the data file existed
    assert "NOT before the data file mtime" in rec["order_reason"]
    assert _lock_rec(tmp_path / "reg")["order_ok"] is False


def test_declared_fetch_earlier_than_file_mtime_is_flagged_inconsistent(tmp_path):
    """A declared fetched_at earlier than the data file's mtime is impossible; it is flagged,
    and the declared value is not what order_ok rests on."""
    from tools import validity_harness as vh
    import os as _os, time as _time
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n", encoding="utf-8")
    future = _time.time() + 7200
    _os.utime(data, (future, future))  # file mtime is well in the future
    rec = vh.register_and_lock(
        str(tmp_path / "reg"), claim={"id": "SYN1"},
        data_fetched_at="2026-10-30T00:00:00+09:00",  # earlier than the file mtime
        data_location=str(data))
    assert rec["fetched_at_inconsistent"] is True
    assert "inconsistent" in rec["order_reason"]
    # The trusted registration (now) is before the future mtime, so the order itself holds.
    assert rec["order_ok"] is True


def test_no_data_file_falls_back_to_declared_times_and_says_so(tmp_path):
    """With no file to check, order_ok rests on the declared fetch time and the record says so,
    rather than pretending the declared value was verified."""
    from tools import validity_harness as vh
    rec = vh.register_and_lock(
        str(tmp_path / "reg"), claim={"id": "SYN1"},
        data_fetched_at="2999-01-01T00:00:00+09:00",  # far future -> trusted now < it
        data_location=None)
    assert rec["data_file_mtime"] is None
    assert rec["order_ok"] is True
    assert "no data file mtime available" in rec["order_reason"]
    assert rec["registered_at_trusted"] is True


def test_engine_unavailable_marker_and_strict_rejection(tmp_path, monkeypatch):
    """When the engine is unavailable the record says 'judge: not run (engine unavailable: ...)'
    and, under MCP_JUDGE_STRICT, a MISSING request is rejected (strict default unchanged)."""
    import os as _os, time as _time
    data = tmp_path / "data.csv"
    data.write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    future = _time.time() + 3600
    _os.utime(data, (future, future))
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
               "registration": {"root": "reg", "claim": {"id": "SYN1"},
                                "registered_at": "2026-10-29T00:00:00+09:00"},
               "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(
        judge_gate, "run_engine_judge",
        lambda *a, **kw: {"engine_available": False, "unavailable_reason": "engine root not found"})
    ok, status = judge_gate.complete_judge_request(GOAL, str(tmp_path))
    assert ok
    result = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert result["engine_available"] is False
    assert result["engine_status"] == \
        "judge: not run (engine unavailable: engine root not found)"
    assert result["unavailable_reason"] == "engine root not found"
    assert "judge NOT run" in result["footer"]
    # Strict default is unchanged: a MISSING request under strict rejects.
    monkeypatch.setenv("MCP_JUDGE_STRICT", "1")
    ok2, status2 = judge_gate.complete_judge_request(GOAL, str(tmp_path / "empty"), strict=True)
    assert ok2 is False and "judge: none" in status2


def test_harness_record_is_not_overwritten_by_a_rerun(tmp_path, monkeypatch):
    """A harness-written judge_result.json is authoritative: a second completion run must not
    clobber it (the worker cannot replace the record by re-running)."""
    import os as _os, time as _time
    data = tmp_path / "data.csv"
    data.write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    future = _time.time() + 3600
    _os.utime(data, (future, future))
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
               "registration": {"root": "reg", "claim": {"id": "SYN1"},
                                "registered_at": "2026-10-29T00:00:00+09:00"},
               "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    judge_gate.complete_judge_request(GOAL, str(tmp_path))
    first = (tmp_path / "judge_result.json").read_text(encoding="utf-8")
    # Tamper with the on-disk record as if a worker tried to replace it, keeping source=harness.
    tampered = json.loads(first)
    tampered["engine_status"] = "TAMPERED"
    (tmp_path / "judge_result.json").write_text(
        json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    judge_gate.complete_judge_request(GOAL, str(tmp_path))  # re-run must not overwrite
    after = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert after["engine_status"] == "TAMPERED"


def test_durable_reason_comes_from_harness_record_not_worker_text(tmp_path, monkeypatch):
    """The outcome reason is the harness pointer to judge_result.json, never text the worker
    put in its reply, and it is not truncated to 200 chars of a worker-authored footer."""
    import os as _os, time as _time
    data = tmp_path / "data.csv"
    data.write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    future = _time.time() + 3600
    _os.utime(data, (future, future))
    request = {"claim_id": "SYN1", "data_location": "data.csv",
               "fetched_at": "2026-10-30T00:00:00+09:00",
               "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
               "registration": {"root": "reg", "claim": {"id": "SYN1"},
                                "registered_at": "2026-10-29T00:00:00+09:00"},
               "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "done"
    # The durable reason is the harness pointer, not worker footer text.
    assert w.reason.startswith("judge: recorded -> judge_result.json |")
    assert "judge: not run (engine unavailable:" in w.reason
    # The full footer still reaches the VISIBLE answer (never silent) ...
    assert "判定器は実行されませんでした" in w.last_response
    # ... and the full footer is preserved intact in the record (not cut at 200 chars).
    result = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert "判定器は実行されませんでした" in result["footer"]
