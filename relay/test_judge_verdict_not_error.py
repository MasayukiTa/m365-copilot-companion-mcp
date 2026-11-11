# -*- coding: utf-8 -*-
"""Test claim schema validation, registration order, and verdict reporting."""
from __future__ import annotations

import json

import pytest

from relay import judge_gate


GOAL = "odbc_query analyze data by group"

# The engine's required claim keys, as the frozen loader lists them (mirrored, engine is READ-ONLY).
_ENGINE_KEYS = ("id", "dataset_id", "data_kind", "response", "exposure", "hierarchy",
                "lot_effect_model", "claim_type", "direction", "delta_min", "mode", "family",
                "assumptions")


def _free_text_claim(claim_id="claim_fixture_01"):
    """Create a free-text claim without engine schema keys."""
    return {"id": claim_id,
            "statement": "The fixture variables have no significant correlation.",
            "data_location": "C:/Users/x/Downloads/_clean.csv"}


def _structured_claim(claim_id="claim_fixture_01"):
    """A claim with every engine-required key present (values are synthetic placeholders; the
    PRESENCE is what the harness screens, the engine still owns value validation)."""
    return {
        "id": claim_id, "dataset_id": "ds_synth", "data_kind": "public",
        "response": {"column": "metric", "type": "binary"},
        "exposure": {"column": "flow_L", "kind": "numeric", "level": "between_lot",
                     "contrast": {"type": "per_unit", "value": 1.0}},
        "hierarchy": {"unit_column": "lot"},
        "lot_effect_model": "random_independent", "claim_type": "superiority",
        "direction": "either",
        "delta_min": {"value": 0.3, "scale": "standardized", "rationale": "r",
                      "source": "preregistration"},
        "mode": "confirmatory", "family": {"id": "metric", "m": 1},
        "assumptions": {"distribution": "gaussian", "balance": "unbalanced"},
    }


def _request(claim, *, claim_id="claim_fixture_01", registered_at="2026-01-05T00:00:00+09:00",
             fetched_at="2026-01-04T00:00:00+09:00"):
    """Create a request containing a claim and analysis results."""
    return {
        "claim_id": claim_id, "data_location": "data.csv", "fetched_at": fetched_at,
        "manifest": {"path": "manifest.json", "dataset_id": "ds_kasure",
                     "result": {"n_products": 2, "overall_rate": 0.5}},
        "registration": {"root": "reg", "claim": claim, "registered_at": registered_at},
        "lot_diagnosis": {"y": [1.0, 0.0], "x": [2.0, 1.0], "lot": ["L1", "L2"]},
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

@pytest.mark.parametrize("claim_id", ["claim_fixture_01", "claim_fixture_01", "claim_fixture_01"])
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
                "finding": "fixture finding", "claim_supported": True, "reasons": ["A_OK"],
                "notes": [], "evidence": {"K": 6}, "hashes": {"input_sha256": "a" * 64},
                "claim_source": "worker-registered via harness registry"}

    monkeypatch.setattr(judge_gate, "run_engine_judge", _engine)
    # registered_at BEFORE fetched_at so order_ok can be True (not the subject of this test)
    _write(tmp_path, _request(_structured_claim(), registered_at="2026-01-01T00:00:00+09:00",
                              fetched_at="2026-01-02T00:00:00+09:00"))
    accept, detail, correction = judge_gate.complete_judge_request(
        GOAL, str(tmp_path), correction_budget=2, correction_attempt=0)
    # no schema-gap correction; the engine ran with the structured claim
    assert seen.get("claim") is not None
    assert correction is None or not any("INPUT_MISSING_KEY" in m for m in correction["messages"])


# --- 3. post_hoc_order_note is honest, not an input error ------------------------------------

def test_post_hoc_note_names_hold_cap_and_not_error():
    order = {"order_ok": False, "registered_at": "2026-01-05T00:00:00+09:00",
             "data_file_mtime": "2026-01-03T00:00:00+09:00"}
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
    order = {"order_ok": False, "registered_at": "2026-01-05T00:00:00+09:00",
             "data_fetched_at": "2026-01-04T00:00:00+09:00",
             "data_file_mtime": "2026-01-03T00:00:00+09:00"}
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
    _write(tmp_path, _request(_structured_claim(), registered_at="2026-01-05T00:00:00+09:00",
                              fetched_at="2026-01-04T00:00:00+09:00"))
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
