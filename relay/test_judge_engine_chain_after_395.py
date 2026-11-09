# -*- coding: utf-8 -*-
"""Regression tests for the three defects visible in job 04daa4a59ba3's judge_result.json
(branch fix/judge-engine-chain-after-395, after PR #395 engaged the final-step judge for a
goal that was only a path to a request folder).

The defects, taken from that job's judge_request.json shape (claim_id 'example_kasure_analysis', a
manifest whose .path was the DATA FOLDER and whose .result was a PATH STRING, and NO
registration section at all):

  1. 'registered claim not found for id example_kasure_analysis' with claim_source null -- the worker
     named a claim id but the request carried no registration.claim and the engine had no
     pre-registered configs/<id>.json, so the engine could not run and the worker was never
     told, in correctable terms, what to send. A worker that DOES follow the documented path
     (include registration.claim with id == claim_id) must make the engine run end to end.
  2. 'manifest.result must be an object ... got str' -- the worker put a path string where the
     analysis-result OBJECT belongs; the correction must show a minimal valid example.
  3. 'manifest: not run: [Errno 13] Permission denied: <request folder>' -- manifest.path was a
     DIRECTORY, so record_manifest opened the folder for writing. The contract must reject a
     directory by name BEFORE the harness opens it, and record_manifest must not open a folder.

HERMETIC: no real folders of the owner. A FAKE engine root (a tiny `python -m validity` that
echoes a JSON verdict) and a FAKE case folder built under tmp_path stand in for everything.
No secret data, measurement row or employee id appears here.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

jg = pytest.importorskip(
    "relay.judge_gate",
    reason="final-step judge harness relay/judge_gate.py not implemented yet",
)
vh = pytest.importorskip(
    "tools.validity_harness",
    reason="orchestration layer tools/validity_harness.py not implemented yet",
)

from relay.judge_gate import complete_judge_request, run_engine_judge

CLAIM_ID = "example_kasure_analysis"

# A minimal analysis-result OBJECT, the shape manifest.result must carry (defect 2). No real
# measurements -- synthetic lot ids and numbers only.
RESULT_OBJECT = {
    "n_lots": 2,
    "kasure_rate_by_lot": {"L-A": 0.5, "L-B": 0.0},
    "top_correlation_vs_kasure": {"feat_x": 0.9},
}


def _fake_engine_root(tmp_path, *, with_configs_id=None):
    """A fake engine checkout whose `python -m validity` echoes a JSON verdict and records the
    --claim path it was handed. Mirrors relay/test_judge_worker_claim_to_engine.py."""
    root = tmp_path / "engine"
    (root / "validity").mkdir(parents=True)
    if with_configs_id:
        (root / "configs").mkdir()
        (root / "configs" / ("%s.json" % with_configs_id)).write_text(
            json.dumps({"id": with_configs_id, "claim_type": "between_lot",
                        "dataset_id": "synthetic", "source": "human-configs"}),
            encoding="utf-8")
    verdict = {"id": CLAIM_ID, "verdict": "HOLD", "verdict_ja": "\u4fdd\u7559",
               "finding": None, "claim_supported": None, "reasons": ["D_FAIL_NO_LOCK"],
               "notes": [], "evidence": {"K": 2}, "hashes": {"input_sha256": "0" * 64}}
    script = (
        "import json, os, sys\n"
        "a = sys.argv\n"
        "def opt(name):\n"
        "    return a[a.index(name) + 1] if name in a else None\n"
        "claim = opt('--claim')\n"
        "rp = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '_received.json')\n"
        "open(rp, 'w', encoding='utf-8').write(json.dumps({'claim': claim}))\n"
        "with open(claim, encoding='utf-8-sig') as f:\n"
        "    c = json.load(f)\n"
        "v = json.loads(%r)\n"
        "v['id'] = c.get('id', v.get('id'))\n"
        "sys.stdout.write(json.dumps(v) + '\\n')\n"
    ) % (json.dumps(verdict),)
    (root / "validity" / "__main__.py").write_text(script, encoding="utf-8")
    (root / "validity" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _case_folder(tmp_path):
    """A fake case/output folder with a data FILE inside it (stands in for the owner's request
    folder, which must never be touched)."""
    out = tmp_path / "job_out"
    out.mkdir()
    data = out / "data.csv"
    data.write_text("lot,x,y\nL-A,1,0.5\nL-B,0,0.0\n", encoding="utf-8")
    return out, data


def _write_request(out, request):
    (out / "judge_request.json").write_text(
        json.dumps(request, ensure_ascii=False), encoding="utf-8")


# ---- defect 2: manifest.result must be an object, with a minimal example -----

def test_manifest_result_path_string_is_corrected_with_example(tmp_path, monkeypatch):
    monkeypatch.setattr(jg, "ENGINE_ROOT", str(_fake_engine_root(tmp_path)))
    out, data = _case_folder(tmp_path)
    # The exact defect-2 shape: result is a PATH STRING, not an object.
    _write_request(out, {
        "claim_id": CLAIM_ID, "data_location": str(data),
        "fetched_at": "2026-11-01T00:00:00+09:00",
        "manifest": {"path": "manifest.json", "dataset_id": CLAIM_ID,
                     "result": str(out / "report.xlsx")},
    })
    accept, detail, correction = complete_judge_request(
        "ADAMS/MCL DB を照会し相関を出す", str(out),
        request_text="ADAMS/MCL DB を照会し相関を出す",
        correction_budget=2, correction_attempt=0)
    assert correction is not None
    joined = "\n".join(correction["messages"])
    assert "manifest.result must be an object" in joined
    assert "not a path string" in joined.lower() or "NOT a path string" in joined
    # A minimal valid example is shown so the worker sees the SHAPE.
    assert "kasure_rate_by_lot" in joined


# ---- defect 3: manifest.path is a directory, caught before any open() --------

def test_manifest_path_directory_is_rejected_by_name_not_errno13(tmp_path, monkeypatch):
    monkeypatch.setattr(jg, "ENGINE_ROOT", str(_fake_engine_root(tmp_path)))
    out, data = _case_folder(tmp_path)
    folder = tmp_path / "case_dir"
    folder.mkdir()
    _write_request(out, {
        "claim_id": CLAIM_ID, "data_location": str(data),
        "fetched_at": "2026-11-01T00:00:00+09:00",
        # path is a DIRECTORY (the exact defect-3 mistake), result now a valid object so only
        # the directory path is wrong.
        "manifest": {"path": str(folder), "dataset_id": CLAIM_ID, "result": RESULT_OBJECT},
    })
    accept, detail, correction = complete_judge_request(
        "ADAMS/MCL DB を照会し相関を出す", str(out),
        request_text="ADAMS/MCL DB を照会し相関を出す",
        correction_budget=2, correction_attempt=0)
    assert correction is not None
    joined = "\n".join(correction["messages"])
    assert "manifest.path" in joined
    assert "directory" in joined.lower()
    # The old Errno 13 permission-denied must NOT appear: the directory is rejected by the
    # contract before record_manifest ever opens anything.
    assert "Permission denied" not in detail
    assert "Errno 13" not in detail


def test_record_manifest_refuses_a_directory_path(tmp_path):
    """The harness primitive itself must not open a directory for writing (defect 3 root)."""
    _, data = _case_folder(tmp_path)
    folder = tmp_path / "a_dir"
    folder.mkdir()
    with pytest.raises(IsADirectoryError):
        vh.record_manifest(str(folder), dataset_id=CLAIM_ID, data_path=str(data),
                           result=RESULT_OBJECT, fetched_at="2026-11-01T00:00:00+09:00")


# ---- defect 1: no claim anywhere -> correctable; with registration -> runs ----

def test_missing_claim_is_a_named_correction_naming_registration_claim(tmp_path, monkeypatch):
    # Fake engine with NO configs/<id>.json, so the only way to supply a claim is registration.
    monkeypatch.setattr(jg, "ENGINE_ROOT", str(_fake_engine_root(tmp_path)))
    out, data = _case_folder(tmp_path)
    # The exact defect-1 shape: a valid manifest, but NO registration section at all.
    _write_request(out, {
        "claim_id": CLAIM_ID, "data_location": str(data),
        "fetched_at": "2026-11-01T00:00:00+09:00",
        "manifest": {"path": "manifest.json", "dataset_id": CLAIM_ID, "result": RESULT_OBJECT},
    })
    accept, detail, correction = complete_judge_request(
        "ADAMS/MCL DB を照会し相関を出す", str(out),
        request_text="ADAMS/MCL DB を照会し相関を出す",
        correction_budget=2, correction_attempt=0)
    assert correction is not None
    joined = "\n".join(correction["messages"])
    # The correction must name EXACTLY what to send: a registration.claim whose id == claim_id.
    assert "registration" in joined
    assert "claim" in joined
    assert CLAIM_ID in joined


def test_worker_following_the_documented_path_makes_the_engine_run(tmp_path, monkeypatch):
    """End to end: a worker that includes registration.claim with id == claim_id makes the
    engine run and a verdict is produced (defect 1 fixed, chain works end to end)."""
    monkeypatch.setattr(jg, "ENGINE_ROOT", str(_fake_engine_root(tmp_path)))
    out, data = _case_folder(tmp_path)
    registry_root = tmp_path / "registry_root"
    registry_root.mkdir()
    _write_request(out, {
        "claim_id": CLAIM_ID, "data_location": str(data),
        "fetched_at": "2026-11-01T00:00:00+09:00",
        "manifest": {"path": "manifest.json", "dataset_id": CLAIM_ID, "result": RESULT_OBJECT},
        "registration": {
            "root": str(registry_root),
            "claim": {"id": CLAIM_ID, "claim_type": "between_lot", "dataset_id": CLAIM_ID,
                      "source": "worker-registered"},
            "registered_at": "2026-10-01T00:00:00+09:00",
        },
    })
    accept, detail, correction = complete_judge_request(
        "ADAMS/MCL DB を照会し相関を出す", str(out),
        request_text="ADAMS/MCL DB を照会し相関を出す",
        correction_budget=2, correction_attempt=0)
    # With a matching registration.claim and no field problems, the engine runs: no correction.
    assert correction is None, detail
    assert "judge: requested" in detail
    # The verdict footer carries the engine's verdict (HOLD), proving the chain ran end to end.
    assert "HOLD" in detail or "\u4fdd\u7559" in detail
    # The authoritative harness record was written and names the worker-registered source.
    rec = json.loads((out / "judge_result.json").read_text(encoding="utf-8"))
    assert rec["engine_available"] is True
    assert rec["claim_source"] and "worker-registered" in rec["claim_source"]


def test_engine_judge_direct_missing_claim_reports_null_source(tmp_path):
    """run_engine_judge with neither a configs claim nor a registered_claim returns the
    structured 'registered claim not found' signal (claim_source null) the gate keys off."""
    root = _fake_engine_root(tmp_path)
    judge_folder = tmp_path / "job"
    judge_folder.mkdir()
    verdict = run_engine_judge(CLAIM_ID, root=str(root), python_exe=sys.executable,
                               registered_claim=None, judge_folder=str(judge_folder))
    assert verdict["engine_available"] is False
    assert verdict.get("claim_source") is None
    assert str(verdict["unavailable_reason"]).startswith("registered claim not found")
