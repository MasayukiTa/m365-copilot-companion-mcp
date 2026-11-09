"""A PRESENT-but-INVALID judge_request.json is corrected by the worker, within a finite budget,
instead of silently ending as 'judge: not run'.

What these tests pin down (the task's acceptance list):
  - an invalid field is named EXACTLY (field, expected shape, what was received);
  - a claim id mismatch names BOTH ids that differ;
  - the worker is sent back once, and a then-valid request is accepted;
  - the budget is finite: once exhausted the completion marker says the request is invalid and,
    under MCP_JUDGE_STRICT, the job is rejected (VERIFY_FAILED);
  - a VALID request whose engine is merely unavailable is NOT sent back (not the worker's fault);
  - an ABSENT request keeps the existing 'judge: none' behaviour unchanged;
  - every correction turn is recorded in judge_result.json (attempt number + messages).
"""
import json

from relay import judge_gate
from relay.relay_fleet import RelayWorker, JUDGE_CORRECTION_BUDGET


GOAL = "odbc_query で検査件数を集計して表にする"


def worker(folder):
    """A minimal worker whose _settle_done can run without the full fleet machinery. The judge
    correction send path uses _task_anchor; we stub it to return the raw nudge so the test reads
    the job text directly, and stub the two no-op hooks _settle_done calls."""
    w = RelayWorker.__new__(RelayWorker)
    w.goal, w.cwd = GOAL, str(folder)
    w.last_response, w.reason, w.job, w.status = "answer", "", "", "waiting"
    w.outcome = ""
    w._judge_correction_attempt = 0
    w._record_tree_stability = lambda: None
    w._claim_verdict = lambda: "DONE"
    w._task_anchor = lambda nudge: nudge
    return w


def _valid_request(claim_id="SYN1"):
    return {"claim_id": claim_id, "data_location": "data.csv",
            "fetched_at": "2026-10-30T00:00:00+09:00",
            "manifest": {"path": "manifest.json", "dataset_id": "ds", "result": {"n": 2}},
            "registration": {"root": "reg", "claim": {"id": claim_id},
                             "registered_at": "2026-10-29T00:00:00+09:00"},
            "lot_diagnosis": {"y": [1, 2], "x": [0, 1], "lot": ["a", "b"]}}


def _write(tmp_path, request):
    (tmp_path / "judge_request.json").write_text(
        json.dumps(request), encoding="utf-8")


# --- 1. the exact field is named ------------------------------------------------------------

def test_invalid_field_is_named_exactly(tmp_path):
    """manifest.result is a string, not an object: the message names the field, the expected
    shape, and the received type -- not a generic 'invalid manifest fields'."""
    request = _valid_request()
    request["manifest"]["result"] = "a summary string"
    _write(tmp_path, request)
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert correction is not None
    assert any("manifest.result must be an object" in m for m in correction["messages"])
    assert any("got str" in m for m in correction["messages"])
    # the expected shape (schema) is handed back alongside the message
    assert "judge_request.json" in correction["schema"]
    # LONG data is never echoed. manifest.result here is the WRONG type (a long string where an
    # object is required); _short_type truncates a scalar string to <=40 chars, so the 5000-char
    # blob can never land in the message -- only a truncated head with an ellipsis and its length.
    request_long = _valid_request()
    request_long["manifest"]["result"] = "X" * 5000  # wrong type AND long
    _write(tmp_path, request_long)
    _a, _d, corr_long = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert corr_long is not None
    joined_long = " ".join(corr_long["messages"])
    assert "manifest.result must be an object" in joined_long
    assert "X" * 5000 not in joined_long  # the full blob is not echoed
    assert "..." in joined_long and "len 5000" in joined_long  # truncated + length instead


# --- 2. a claim id mismatch names BOTH ids --------------------------------------------------

def test_claim_id_mismatch_names_both_ids(tmp_path):
    request = _valid_request(claim_id="SYN1")
    request["registration"]["claim"]["id"] = "OTHER-2"
    _write(tmp_path, request)
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert correction is not None
    joined = " ".join(correction["messages"])
    assert "registration.claim.id" in joined
    assert "OTHER-2" in joined and "SYN1" in joined


# --- 3. sent back once, then a valid request is accepted ------------------------------------

def test_correction_sent_then_valid_request_accepted(tmp_path, monkeypatch):
    """First settle: an invalid request sends the worker back to ready with the fix job (not
    DONE). The worker then writes a VALID request; the second settle accepts and completes."""
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    bad = _valid_request()
    bad["lot_diagnosis"] = {"y": [1, 2], "x": [0], "lot": ["a", "b"]}  # lengths differ
    _write(tmp_path, bad)
    w = worker(tmp_path)
    w._settle_done()
    # sent back, NOT done
    assert w.status == "ready" and w.outcome != "DONE"
    assert w._judge_correction_attempt == 1
    assert "judge_request.json" in w.job  # the fix job carries the schema/instruction
    assert "lot_diagnosis" in w.job and "same length" in w.job
    # the correction turn is recorded durably
    rec = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert rec["source"] == "harness-correction"
    assert rec["corrections"][0]["attempt"] == 1
    assert any("lot_diagnosis" in m for m in rec["corrections"][0]["messages"])
    # the worker fixes the request and settles again -> accepted, DONE
    _write(tmp_path, _valid_request())
    w.status = "waiting"
    w._settle_done()
    assert w.status == "done" and w.outcome == "DONE"
    # the authoritative harness record now exists and carries the correction history forward
    rec2 = json.loads((tmp_path / "judge_result.json").read_text(encoding="utf-8"))
    assert rec2["source"] == "harness"
    assert rec2["corrections"][0]["attempt"] == 1


# --- 4. budget exhausted -> marker + strict rejection ---------------------------------------

def test_budget_exhausted_marker_and_strict_rejection(tmp_path, monkeypatch):
    """After the budget is spent an invalid request is no longer sent back: the completion marker
    says 'judge: request invalid (<messages>)' and, under strict, the job is VERIFY_FAILED."""
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    request = _valid_request()
    request["manifest"]["dataset_id"] = 123  # wrong type, not fixed across turns
    _write(tmp_path, request)
    # At the final attempt (== budget), the 3-tuple is returned with exhausted=True, the marker
    # is in the visible text, and under strict accept is False.
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), strict=True,
        correction_budget=JUDGE_CORRECTION_BUDGET,
        correction_attempt=JUDGE_CORRECTION_BUDGET)
    assert correction is not None and correction.get("exhausted") is True
    assert "judge: request invalid (" in detail
    assert "manifest.dataset_id" in detail
    assert accept is False  # strict rejects
    # non-strict at exhaustion accepts but still carries the marker
    accept2, detail2, correction2 = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), strict=False,
        correction_budget=JUDGE_CORRECTION_BUDGET,
        correction_attempt=JUDGE_CORRECTION_BUDGET)
    assert accept2 is True and "judge: request invalid (" in detail2


def test_budget_exhausted_through_settle_strict_is_rejected(tmp_path, monkeypatch):
    """End-to-end through the worker: with the attempt counter already at the budget, _settle_done
    does NOT send back again; under strict it records VERIFY_FAILED."""
    monkeypatch.setenv("MCP_JUDGE_STRICT", "1")
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    request = _valid_request()
    request["manifest"]["dataset_id"] = 123
    _write(tmp_path, request)
    w = worker(tmp_path)
    w._judge_correction_attempt = JUDGE_CORRECTION_BUDGET  # budget already spent
    w._settle_done()
    assert w.status == "stuck" and w.outcome == "VERIFY_FAILED"
    assert "judge: request invalid" in w.reason


# --- 5. a valid request whose engine is unavailable is NOT sent back ------------------------

def test_engine_unavailable_is_not_sent_back(tmp_path, monkeypatch):
    """The request is VALID; only the engine is unavailable (not the worker's fault). correction
    is None and the worker completes rather than being sent back to 'fix' a request that is fine.
    """
    monkeypatch.setattr(
        judge_gate, "run_engine_judge",
        lambda *a, **kw: {"engine_available": False, "unavailable_reason": "engine root not found"})
    _write(tmp_path, _valid_request())
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert accept is True and correction is None
    # and through the worker: it settles DONE, not ready
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "done" and w.outcome == "DONE"
    assert w._judge_correction_attempt == 0  # never incremented


# --- 6. an absent request is unchanged ------------------------------------------------------

def test_absent_request_unchanged(tmp_path, monkeypatch):
    """No judge_request.json present: the existing 'judge: none' behaviour is kept, and no
    correction is offered (an absent request is not a correctable one)."""
    monkeypatch.delenv("MCP_JUDGE_STRICT", raising=False)
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert accept is True and detail == "judge: none" and correction is None
    # strict keeps the existing missing-request rejection, still with no correction
    accept2, detail2, correction2 = judge_gate.complete_judge_request(
        GOAL, str(tmp_path / "empty"), strict=True,
        correction_budget=2, correction_attempt=0)
    assert accept2 is False and "judge: none" in detail2 and correction2 is None
    # and through the worker the absent-request path is byte-for-byte the old behaviour
    w = worker(tmp_path)
    w._settle_done()
    assert w.status == "done" and "judge: none" in w.reason


def test_two_tuple_contract_unchanged_without_budget(tmp_path, monkeypatch):
    """Without correction_budget the function keeps its original 2-tuple return, so every caller
    that does not opt in is untouched."""
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda *a, **kw: {"engine_available": False})
    _write(tmp_path, _valid_request())
    result = judge_gate.complete_judge_request(GOAL, str(tmp_path))
    assert isinstance(result, tuple) and len(result) == 2
