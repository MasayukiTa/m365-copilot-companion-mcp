# -*- coding: utf-8 -*-
"""The judge must not deliver a bare verdict=ERROR / INPUT_MISSING_KEY for a worker's analysis
when the real problem is that the worker's free-text claim was never mapped into the engine's
structured claim shape -- and a post-hoc (late) registration must be shown HONESTLY as a HOLD-cap
condition (D_FAIL_LOCK_AFTER_DATA), never mislabelled as an input error.

WHY THIS EXISTS (measured 2026-11-02). Three real judge folders (example_analysis, example_analysis,
example_analysis) each delivered verdict=ERROR, reason INPUT_MISSING_KEY, note
LOT_EFFECT_MODEL_DECLARED_NOT_TESTABLE, loo_n=0, K=null, and order_ok=false. The engine input in
each (engine_claim_<id>.json) carried only {id, statement, data_location}: a free-text claim. The
engine's claim loader REQUIRES 12+ structured keys (validity/claim.py REQUIRED_KEYS plus
assumptions), so it returned ERROR before any analysis ran. PR #400 wired the worker claim to the
engine but never mapped it into the engine shape, and its tests used an already-structured fake
claim, so the gap was invisible.

WHAT THESE TESTS PIN.
  - engine_claim_schema_gap names exactly which engine-required keys a free-text claim lacks, and
    is empty for a structured claim; it never fabricates values.
  - At completion, a worker claim that lacks engine keys produces a correction that NAMES the
    missing keys and the expected shape -- BEFORE the engine is run -- instead of an opaque
    INPUT_MISSING_KEY. The three real folder shapes are reproduced as hermetic fixtures.
  - post_hoc_order_note states a late registration plainly: it is NOT an input error, the engine
    rule caps it at HOLD (D_FAIL_LOCK_AFTER_DATA), and it names what would make it valid. It is
    empty when the order is fine.
  - The footer shows the post-hoc line for order_ok=False and never calls it ERROR.
  - When the engine itself returns ERROR for a genuinely structured-but-late claim, the harness
    surfaces the engine's own reason (not hidden) alongside the post-hoc explanation.

All fixtures are synthetic: no secret engine, no measurement row, no employee id. The engine is
not invoked (run_engine_judge is stubbed) except where a test explicitly checks the pass-through
of an engine-returned verdict.
"""
from __future__ import annotations

import json

import pytest

from relay import judge_gate


GOAL = "odbc_query で DBB のロット別かすれ率を集計して相関を出す"

# The engine's required claim keys, as the frozen loader lists them (mirrored, engine is READ-ONLY).
_ENGINE_KEYS = ("id", "dataset_id", "data_kind", "response", "exposure", "hierarchy",
                "lot_effect_model", "claim_type", "direction", "delta_min", "mode", "family",
                "assumptions")


def _free_text_claim(claim_id="example_analysis"):
    """The real shape a worker registered: a free-text statement, no engine schema keys."""
    return {"id": claim_id,
            "statement": "679端部かすれは来歴データと有意な相関を示さない(全因子95%CIが0をまたぐ)。",
            "data_location": "C:/Users/x/Downloads/_clean.csv"}


def _structured_claim(claim_id="example_analysis"):
    """A claim with every engine-required key present (values are synthetic placeholders; the
    PRESENCE is what the harness screens, the engine still owns value validation)."""
    return {
        "id": claim_id, "dataset_id": "ds_kasure", "data_kind": "public",
        "response": {"column": "kasure", "type": "binary"},
        "exposure": {"column": "flow_L", "kind": "numeric", "level": "between_lot",
                     "contrast": {"type": "per_unit", "value": 1.0}},
        "hierarchy": {"unit_column": "lot"},
        "lot_effect_model": "random_independent", "claim_type": "superiority",
        "direction": "either",
        "delta_min": {"value": 0.3, "scale": "standardized", "rationale": "r",
                      "source": "preregistration"},
        "mode": "confirmatory", "family": {"id": "kasure", "m": 1},
        "assumptions": {"distribution": "gaussian", "balance": "unbalanced"},
    }


def _request(claim, *, claim_id="example_analysis", registered_at="2026-11-02T10:34:20+09:00",
             fetched_at="2026-11-02T01:26:07+09:00"):
    """A judge_request shaped like the three real folders: free-text-or-structured claim plus the
    analysis the worker DID produce (manifest.result, lot_diagnosis y/x/lot)."""
    return {
        "claim_id": claim_id, "data_location": "data.csv", "fetched_at": fetched_at,
        "manifest": {"path": "manifest.json", "dataset_id": "ds_kasure",
                     "result": {"n_products": 71, "overall_rate": 0.4085}},
        "registration": {"root": "reg", "claim": claim, "registered_at": registered_at},
        "lot_diagnosis": {"y": [0.9091, 0.0], "x": [90.0, 64.0], "lot": ["T5-07-10NA", "T5-02-03NA"]},
    }


def _write(tmp_path, request):
    (tmp_path / "judge_request.json").write_text(json.dumps(request), encoding="utf-8")


# --- 1. engine_claim_schema_gap -------------------------------------------------------------

def test_gap_lists_every_missing_key_for_a_free_text_claim():
    gap = judge_gate.engine_claim_schema_gap(_free_text_claim())
    # a free-text {id, statement, data_location} claim has id but lacks every other engine key
    assert "id" not in gap
    for k in _ENGINE_KEYS:
        if k != "id":
            assert k in gap, "expected %s to be reported missing" % k


def test_gap_is_empty_for_a_structured_claim():
    assert judge_gate.engine_claim_schema_gap(_structured_claim()) == []


def test_gap_is_empty_for_a_partially_structured_claim():
    """A claim that already carries ANY engine structured key (here it drops 'assumptions') is the
    worker's own attempt at the engine shape: the harness does NOT screen it -- it flows to the
    frozen engine, which owns value validation and would itself report the single missing key. So
    the schema screen returns [] and never second-guesses a structured claim."""
    claim = _structured_claim()
    del claim["assumptions"]
    assert judge_gate.engine_claim_schema_gap(claim) == []


def test_gap_is_empty_for_a_minimal_id_only_claim():
    """A minimal {id}-only claim (no free-text marker, no structured keys) is passed to the engine
    untouched -- the engine reports 'registered claim not found'/INPUT_MISSING_KEY itself. The
    free-text screen only fires for an unambiguous {id, statement, data_location} shape."""
    assert judge_gate.engine_claim_schema_gap({"id": "SYN1"}) == []


def test_gap_is_empty_on_a_non_dict():
    assert judge_gate.engine_claim_schema_gap(None) == []


# --- 2. completion names the missing keys BEFORE the engine runs ----------------------------

@pytest.mark.parametrize("claim_id", ["example_analysis", "example_analysis", "example_analysis"])
def test_free_text_claim_is_corrected_before_engine_with_named_keys(tmp_path, monkeypatch, claim_id):
    """Reproduces the three real folders: a free-text claim + the analysis data. The harness must
    hand back a correction that NAMES the missing engine keys, and must NOT have run the engine."""
    ran = {"engine": False}

    def _no_engine(*a, **kw):
        ran["engine"] = True
        return {"engine_available": False}

    monkeypatch.setattr(judge_gate, "run_engine_judge", _no_engine)
    _write(tmp_path, _request(_free_text_claim(claim_id), claim_id=claim_id))
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    assert correction is not None
    joined = " ".join(correction["messages"])
    assert "INPUT_MISSING_KEY" in joined          # names the engine failure it prevents
    assert "dataset_id" in joined and "response" in joined and "exposure" in joined
    assert "assumptions" in joined and "lot_effect_model" in joined
    # the engine must NOT have been run with the unmapped claim
    assert ran["engine"] is False


def test_structured_claim_does_not_trigger_the_schema_correction(tmp_path, monkeypatch):
    """A fully structured claim passes the schema screen and reaches the engine (here stubbed to a
    GO verdict). No schema-gap correction is raised for it."""
    seen = {}

    def _engine(cid, **kw):
        seen["claim"] = kw.get("registered_claim")
        return {"engine_available": True, "verdict": "GO", "verdict_ja": "採用",
                "finding": "PRACTICAL_NULL", "claim_supported": True, "reasons": ["A_OK"],
                "notes": [], "evidence": {"K": 6}, "hashes": {"input_sha256": "a" * 64},
                "claim_source": "worker-registered via harness registry"}

    monkeypatch.setattr(judge_gate, "run_engine_judge", _engine)
    # registered_at BEFORE fetched_at so order_ok can be True (not the subject of this test)
    _write(tmp_path, _request(_structured_claim(), registered_at="2026-10-01T00:00:00+09:00",
                              fetched_at="2026-10-30T00:00:00+09:00"))
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    # no schema-gap correction; the engine ran with the structured claim
    assert seen.get("claim") is not None
    assert correction is None or not any("INPUT_MISSING_KEY" in m for m in correction["messages"])


# --- 3. post_hoc_order_note is honest, not an input error ------------------------------------

def test_post_hoc_note_names_hold_cap_and_not_error():
    order = {"order_ok": False, "registered_at": "2026-11-02T10:34:20+09:00",
             "data_file_mtime": "2026-10-30T13:58:58+09:00"}
    note = judge_gate.post_hoc_order_note(order)
    assert note
    assert "post-hoc" in note or "事後登録" in note
    assert "D_FAIL_LOCK_AFTER_DATA" in note      # the engine's own rule class
    assert "HOLD" in note or "保留" in note       # capped, not ERROR
    # it explains what would make it valid, without faking the order
    assert "前に" in note


def test_post_hoc_note_is_empty_when_order_is_ok():
    assert judge_gate.post_hoc_order_note(
        {"order_ok": True, "registered_at": "a", "data_file_mtime": "b"}) == ""
    assert judge_gate.post_hoc_order_note(None) == ""


def test_footer_shows_post_hoc_and_never_calls_it_error():
    order = {"order_ok": False, "registered_at": "2026-11-02T10:34:20+09:00",
             "data_fetched_at": "2026-11-02T01:26:07+09:00",
             "data_file_mtime": "2026-10-30T13:58:58+09:00"}
    # engine unavailable here: the footer still must carry the honest post-hoc explanation
    footer = judge_gate.build_judge_footer(
        verdict={"unavailable_reason": "x"}, engine_available=False, order_record=order)
    assert "order_ok=False" in footer
    assert "D_FAIL_LOCK_AFTER_DATA" in footer
    assert ("事後登録" in footer) or ("post-hoc" in footer)


def test_engine_error_for_a_late_structured_claim_is_surfaced_not_hidden(tmp_path, monkeypatch):
    """If the engine itself returns a verdict (ERROR or HOLD) for a structured but late claim, the
    harness surfaces the engine's own reason in the footer AND adds the post-hoc explanation --
    neither is hidden."""
    def _engine(cid, **kw):
        return {"engine_available": True, "verdict": "HOLD", "verdict_ja": "保留",
                "finding": None, "claim_supported": None,
                "reasons": ["D_FAIL_LOCK_AFTER_DATA"], "notes": [], "evidence": {"K": 6},
                "hashes": {"input_sha256": "b" * 64},
                "claim_source": "worker-registered via harness registry"}

    monkeypatch.setattr(judge_gate, "run_engine_judge", _engine)
    # structured claim, but registered AFTER the data (post-hoc)
    _write(tmp_path, _request(_structured_claim(), registered_at="2026-11-02T10:34:20+09:00",
                              fetched_at="2026-11-02T01:26:07+09:00"))
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    # the engine's own HOLD + reason is surfaced, and the post-hoc note is present, not hidden
    assert "D_FAIL_LOCK_AFTER_DATA" in detail
    assert ("事後登録" in detail) or ("post-hoc" in detail)
    # a HOLD is not an ERROR: the engine's verdict line reports a HOLD, not an input-missing ERROR.
    # (The post-hoc explanation itself states it is NOT an input-missing ERROR, to say it is NOT an input
    # error, so we check the verdict LINE, not the whole text, for the ERROR label.)
    verdict_line = [ln for ln in detail.splitlines() if ln.startswith("判定:")]
    assert verdict_line and "HOLD" in verdict_line[0] and "保留" in verdict_line[0]
    assert "入力不備" not in verdict_line[0]
