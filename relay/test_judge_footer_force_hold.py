# -*- coding: utf-8 -*-
"""Pin the DESIGN FORCE-HOLD wiring in relay/judge_gate.py (slice 3/8).

WHAT THIS SLICE OWNS, AND WHAT IT DOES NOT. Task 1 (feat/design-feasibility-core)
produces a design_feasibility RESULT by inspecting the comparison design WITHOUT
reading the response/outcome column. THIS slice is the WIRING that consumes that
result at the final step: when the design is infeasible, the FINAL DISPLAY is forced
to a design-infeasible HOLD regardless of the engine verdict, the reason code and what was
seen (common strata, per-group lot counts, overlap coefficient) are named on the
the judge footer, and the engine's ORIGINAL output is preserved unchanged (footer: labelled
as the original engine output, unchanged; record: separate key design_feasibility_hold + top-level final_display,
with `verdict` untouched).

These tests consume a plain dict (the contract shape), so they are independent of
task 1's internals -- they never import or run the upstream check. The force is
stricter-only: only feasible is EXACTLY False forces a hold; feasible True / None /
missing is a byte-identical pass-through. A separate path from the info-only
`design_check` -- distinct keys (design_feasibility_hold / final_display).
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from relay import judge_gate as jg  # noqa: E402


_ENGINE_GO = {
    "id": "judge_footer_force_hold",
    "verdict": "GO",
    "verdict_ja": "採用",
    "finding": "effect present",
    "claim_supported": True,
    "reasons": ["R_OK"],
    "notes": ["n1"],
    "evidence": {"n": 10, "K": 3, "ci": [0.1, 0.5]},
    "hashes": {"input_sha256": "deadbeef"},
}


def _infeasible(**over):
    d = {
        "feasible": False,
        "reason_code": "NO_COMMON_STRATUM",
        "common_strata": 0,
        "lots_per_group": {"A": 12, "B": 9},
        "overlap_coefficient": 0.0,
    }
    d.update(over)
    return d


def test_hold_none_for_missing_feasible_or_true():
    assert jg.design_feasibility_hold(None) is None
    assert jg.design_feasibility_hold({}) is None
    assert jg.design_feasibility_hold({"feasible": True}) is None
    assert jg.design_feasibility_hold({"feasible": None}) is None
    assert jg.design_feasibility_hold("infeasible") is None


def test_hold_record_carries_reason_and_seen():
    h = jg.design_feasibility_hold(_infeasible(extra="keep-me"))
    assert h["forced"] is True
    assert h["reason_code"] == "NO_COMMON_STRATUM"
    assert h["common_strata"] == 0
    assert h["lots_per_group"] == {"A": 12, "B": 9}
    assert h["overlap_coefficient"] == 0.0
    assert h["seen"] == {"extra": "keep-me"}


def test_hold_unknown_reason_code_surfaced_verbatim():
    h = jg.design_feasibility_hold(_infeasible(reason_code="SOMETHING_NEW"))
    assert h["reason_code"] == "SOMETHING_NEW"


def test_hold_default_reason_code_when_absent():
    h = jg.design_feasibility_hold({"feasible": False})
    assert h["forced"] is True
    assert h["reason_code"] == "DESIGN_INFEASIBLE"


def test_footer_forces_hold_and_preserves_engine_output():
    d = _infeasible()
    footer = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True,
                                   design_feasibility=d)
    assert jg.DESIGN_HOLD_DISPLAY in footer
    assert "最終表示: " + jg.DESIGN_HOLD_DISPLAY in footer
    assert "NO_COMMON_STRATUM" in footer
    assert "共通層の数=0" in footer
    assert "A=12" in footer and "B=9" in footer
    assert "重なり係数=0.0" in footer
    assert "判定器の元の出力(参考・変更なし)" in footer
    assert "GO" in footer and "採用" in footer
    assert "R_OK" in footer
    assert footer.index(jg.DESIGN_HOLD_DISPLAY) < footer.index("参考・変更なし")
    assert footer.index("参考・変更なし") < footer.index("GO")


def test_footer_feasible_is_byte_identical_passthrough():
    base = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True)
    feasible = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True,
                                     design_feasibility={"feasible": True})
    missing = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True,
                                    design_feasibility=None)
    assert feasible == base
    assert missing == base
    assert jg.DESIGN_HOLD_DISPLAY not in base


def test_footer_hold_even_when_engine_unavailable():
    footer = jg.build_judge_footer(verdict={"unavailable_reason": "engine root not found"},
                                   engine_available=False, design_feasibility=_infeasible())
    assert jg.DESIGN_HOLD_DISPLAY in footer
    assert "判定器は実行されませんでした" in footer


def test_design_decision_ignores_response_column_values():
    a = _infeasible(response=[1, 2, 3], outcome_mean=0.42)
    b = _infeasible(response=[9, 9, 9], outcome_mean=99.9)
    ha = jg.design_feasibility_hold(a)
    hb = jg.design_feasibility_hold(b)
    for k in ("forced", "reason_code", "common_strata", "lots_per_group",
              "overlap_coefficient"):
        assert ha[k] == hb[k]
    fa = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True, design_feasibility=a)
    fb = jg.build_judge_footer(verdict=_ENGINE_GO, engine_available=True, design_feasibility=b)
    assert fa.split("判定器の元の出力")[0] == fb.split("判定器の元の出力")[0]


def test_write_records_hold_under_separate_key_and_preserves_verdict(tmp_path):
    folder = str(tmp_path)
    jg._write_judge_result(folder, claim_id="judge_footer_force_hold", statuses=["s1"],
                           footer="F", verdict=_ENGINE_GO, engine_available=True,
                           order_record=None, design_feasibility=_infeasible())
    rec = json.load(open(os.path.join(folder, "judge_result.json"), encoding="utf-8-sig"))
    assert rec["final_display"] == jg.DESIGN_HOLD_DISPLAY
    assert rec["design_feasibility_hold"]["reason_code"] == "NO_COMMON_STRATUM"
    assert rec["design_feasibility_hold"]["common_strata"] == 0
    assert rec["verdict"] == _ENGINE_GO
    assert rec["source"] == "harness"


def test_write_feasible_leaves_engine_verdict_as_final_display(tmp_path):
    folder = str(tmp_path)
    jg._write_judge_result(folder, claim_id="judge_footer_force_hold", statuses=["s1"],
                           footer="F", verdict=_ENGINE_GO, engine_available=True,
                           order_record=None, design_feasibility={"feasible": True})
    rec = json.load(open(os.path.join(folder, "judge_result.json"), encoding="utf-8-sig"))
    assert rec["design_feasibility_hold"] is None
    assert rec["final_display"] == "GO"
    assert rec["verdict"] == _ENGINE_GO


def test_write_no_design_section_is_backward_compatible(tmp_path):
    folder = str(tmp_path)
    jg._write_judge_result(folder, claim_id="judge_footer_force_hold", statuses=["s1"],
                           footer="F", verdict=_ENGINE_GO, engine_available=True,
                           order_record=None)
    rec = json.load(open(os.path.join(folder, "judge_result.json"), encoding="utf-8-sig"))
    assert rec["design_feasibility_hold"] is None
    assert rec["final_display"] == "GO"
