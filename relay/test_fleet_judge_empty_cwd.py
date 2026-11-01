# -*- coding: utf-8 -*-
"""The empty-output-folder precondition that silently skipped the final-step judge.

Job 81cac23dfc28 was a data-analysis goal (ADAMS request behind a short case reference) whose
worker cwd was empty. The injected "write judge_request.json in the output folder ..."
instruction was gated on ``self.cwd`` being truthy, so with an empty cwd nothing was injected
and completion passed an empty folder to the judge gate -- the quiet marker "judge: none".

These tests pin the fix: a data-analysis goal with no explicit cwd gets a stable per-job output
folder; the SAME folder is named in the sent prompt and checked at completion; an explicit cwd
is preserved; a directory-creation failure is reported (not silent); and a request-read failure
falls back to the case-lookup excerpt as classification evidence.
"""
import json

import pytest

from relay import judge_gate, relay_fleet, temp_home
from relay.relay_fleet import RelayWorker
from tools import search_ops

GOAL = "\u679d\u3055\u3093_679\u304b\u3059\u308c\u8abf\u67fb\u3000\u3053\u308c\u8abf\u3079\u3066"
# An ADAMS data request -- short reference resolves to this file; its text is what classifies.
ADAMS_REQUEST = ("\u3053\u306e679\u304b\u3059\u308c\u306b\u95a2\u3057\u3066\u3001"
                 "\u3042\u3089\u3086\u308b\u8a2d\u5099\u3001\u6750\u6599\u30c7\u30fc\u30bf\u3092"
                 "\u53d6\u5f97\u3057\u3001ADAMS\u30c7\u30fc\u30bf\u3068\u5b9f\u30c7\u30fc\u30bf\u3092"
                 "\u53c2\u7167\u3002\u30ed\u30c3\u30c8\u5225\u306e\u76f8\u95a2\u3092\u8abf\u3079\u308b\u3002")


def _point_temp_home(monkeypatch, tmp_path):
    """Send temp_home (and thus temp_dir) under tmp_path so per-job dirs are created in temp."""
    monkeypatch.setattr(temp_home, "system_temp", lambda: str(tmp_path))


def _case(tmp_path, request_text=ADAMS_REQUEST):
    case = tmp_path / "case" / "\u679d\u3055\u3093_679\u304b\u3059\u308c\u8abf\u67fb"
    case.mkdir(parents=True)
    (case / "\u3084\u308a\u305f\u3044\u3053\u3068.txt").write_text(request_text, encoding="utf-8")
    return case


def test_empty_cwd_injects_instruction_with_absolute_folder(tmp_path, monkeypatch):
    _point_temp_home(monkeypatch, tmp_path)
    case = _case(tmp_path)
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path / "case"]))
    w = RelayWorker(GOAL, "w0")
    # Classification succeeded from the request text, and with an empty cwd a per-job folder
    # was resolved and injected.
    assert judge_gate.is_data_analysis_goal(GOAL, w._judge_request_text)
    assert w._judge_output_dir
    import os
    assert os.path.isabs(w._judge_output_dir) and os.path.isdir(w._judge_output_dir)
    assert "write judge_request.json in the output folder %s" % w._judge_output_dir in w.job
    # The resolved folder lives under the swept temp home, keyed per job.
    assert os.path.join("m365-companion", "agents", "judge") in w._judge_output_dir


def test_completion_checks_the_same_injected_folder(tmp_path, monkeypatch):
    _point_temp_home(monkeypatch, tmp_path)
    case = _case(tmp_path)
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path / "case"]))
    monkeypatch.delenv("MCP_JUDGE_STRICT", raising=False)
    w = RelayWorker(GOAL, "w0")
    out = w._judge_output_dir
    # Make _settle_done callable without the rest of a live worker.
    w.last_response, w.reason = "answer", ""
    w._record_tree_stability = lambda: None
    w._claim_verdict = lambda: "DONE"
    w._judge_tool_names = lambda: set()

    # Absent file -> judge: none (non-strict), and completion looked in the injected folder.
    w._settle_done()
    assert w.status == "done" and "judge: none" in w.last_response

    # Present file in that SAME folder -> the judge is called.
    request = {"claim_id": "SYN1", "data_location": str(tmp_path / "data.csv"),
               "fetched_at": "2026-10-30T00:00:00+09:00"}
    (__import__("pathlib").Path(out) / "judge_request.json").write_text(
        json.dumps(request), encoding="utf-8")
    calls = []
    monkeypatch.setattr(judge_gate, "run_engine_judge",
                        lambda cid, **kw: calls.append(cid) or {"engine_available": True,
                                                                "id": cid, "verdict": "HOLD"})
    w.last_response, w.reason = "answer", ""
    w._settle_done()
    assert calls == ["SYN1"]
    assert "HOLD" in w.last_response


def test_explicit_cwd_is_preserved(tmp_path, monkeypatch):
    _point_temp_home(monkeypatch, tmp_path)
    case = _case(tmp_path)
    explicit = tmp_path / "explicit_out"
    explicit.mkdir()
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path / "case"]))
    w = RelayWorker({"text": GOAL, "cwd": str(explicit)}, "w0")
    assert w._judge_output_dir == str(explicit)
    assert "write judge_request.json in the output folder %s" % str(explicit) in w.job
    # No per-job temp folder was substituted for an explicit cwd.
    import os
    assert os.path.join("m365-companion", "agents", "judge") not in w._judge_output_dir


def test_directory_creation_failure_is_reported(tmp_path, monkeypatch):
    _point_temp_home(monkeypatch, tmp_path)
    case = _case(tmp_path)
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path / "case"]))
    # Force the per-job directory resolution to fail (temp_dir returns a non-directory).
    monkeypatch.setattr(temp_home, "temp_dir", lambda name: "")
    w = RelayWorker(GOAL, "w0")
    assert not w._judge_output_dir
    assert w._judge_output_error
    assert "could not create per-job judge output directory" in w._judge_output_error
    # The reason is visible on the composed goal, not silently dropped.
    assert "judge step could not be set up" in w.job


def test_request_read_failure_falls_back_to_excerpt(tmp_path, monkeypatch):
    _point_temp_home(monkeypatch, tmp_path)
    case = _case(tmp_path)
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path / "case"]))
    # Make the classifier read of the resolved request fail; the excerpt must stand in.
    real_open = open
    reqfile = str(case / "\u3084\u308a\u305f\u3044\u3053\u3068.txt")

    def flaky_open(path, *a, **kw):
        if str(path) == reqfile:
            raise OSError("boom")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", flaky_open)
    w = RelayWorker(GOAL, "w0")
    assert w._judge_request_read_error
    assert "request read failed" in w._judge_request_read_error
    # The bounded excerpt (first 600 chars) was used as evidence and still classifies.
    assert w._judge_request_text
    assert judge_gate.is_data_analysis_goal(GOAL, w._judge_request_text)
