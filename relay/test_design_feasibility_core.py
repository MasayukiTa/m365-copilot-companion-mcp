# -*- coding: utf-8 -*-
"""Unit tests for judge_gate.design_feasibility -- the result-blind design gate core.

These pin the three conditions of the design gate: the comparison design is
judged WITHOUT the response column, and a two-group exposure contrast is held as
a design-infeasible HOLD when (a) there is no stratifier information, (b) the groups share no
stratum or too few common strata clear k_min lots in BOTH groups, or (c) the groups'
stratum mixes barely overlap (Overlap coefficient < threshold).

HERMETIC: standard library only; no file, network, OS, Edge, M365 or pywin32 access;
no Windows-specific assertion.  design_feasibility takes only lot-count dicts, so the
proof that it is result-blind is structural: it has no response parameter at all.
"""
import importlib

try:
    from relay import judge_gate as jg
except Exception:  # pragma: no cover - allow flat import when run as a script
    import judge_gate as jg  # type: ignore

importlib.reload(jg)


# --- (a) NO_STRATIFIER ------------------------------------------------------
def test_empty_mapping_is_no_stratifier():
    r = jg.design_feasibility({})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "NO_STRATIFIER"


def test_single_group_is_no_stratifier():
    r = jg.design_feasibility({"F": {"P1": 10}})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "NO_STRATIFIER"


def test_both_sides_empty_is_no_stratifier():
    r = jg.design_feasibility({"F": {}, "O": {}})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "NO_STRATIFIER"


# --- (b) NO_COMMON_STRATUM --------------------------------------------------
def test_groups_share_no_product_is_no_common_stratum():
    # Disjoint shape: the two groups share zero stratum level.
    r = jg.design_feasibility({"F": {"P1": 10, "P2": 9}, "O": {"P3": 10, "P4": 9}})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "NO_COMMON_STRATUM"
    assert r["seen"]["n_common_strata"] == 0


# --- (b) INSUFFICIENT_LOTS_IN_COMMON ----------------------------------------
def test_common_product_but_below_kmin_in_one_group():
    # One shared product, but the O group has only 3 lots in it (< k_min 8), and
    # no other covered common stratum, so nothing can be stratified.
    r = jg.design_feasibility({"F": {"P1": 30, "P2": 20}, "O": {"P1": 3, "P5": 20}})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "INSUFFICIENT_LOTS_IN_COMMON"
    assert r["seen"]["n_common_strata"] == 1
    assert r["seen"]["n_common_strata_covered"] == 0


def test_insufficient_requires_min_common_strata_covered():
    # Two shared products, each covered (>= 8 in both). Raising the requirement to
    # 3 covered common strata makes it INSUFFICIENT even though the mix overlaps.
    gsl = {"F": {"P1": 20, "P2": 20}, "O": {"P1": 20, "P2": 20}}
    r = jg.design_feasibility(gsl, min_common_strata=3)
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "INSUFFICIENT_LOTS_IN_COMMON"
    assert r["seen"]["n_common_strata_covered"] == 2


# --- (c) HIGH_SKEW ----------------------------------------------------------
def test_covered_common_but_mixes_barely_overlap_is_high_skew():
    # P1 is shared and covered in both (10 >= 8), but each group's mass is
    # dominated by a DIFFERENT non-shared product, so the mixes barely overlap.
    r = jg.design_feasibility({"F": {"P1": 10, "P2": 200}, "O": {"P1": 10, "P3": 200}})
    assert r["design_infeasible"] is True
    assert r["reason_code"] == "HIGH_SKEW"
    assert r["seen"]["overlap_coefficient"] < jg.DESIGN_OVERLAP_MIN


# --- feasible ---------------------------------------------------------------
def test_identical_well_covered_mix_is_feasible():
    r = jg.design_feasibility({"F": {"P1": 50, "P2": 10}, "O": {"P1": 50, "P2": 10}})
    assert r["design_infeasible"] is False
    assert r["reason_code"] is None
    assert r["seen"]["overlap_coefficient"] == 1.0
    assert r["seen"]["n_common_strata_covered"] >= 1


# --- many strata, one common at k_min, overlap ~0 ---------------------------
def test_many_strata_one_common_is_infeasible():
    # One group is concentrated in a handful of strata, the other in a disjoint
    # many, with a single stratum shared at depth. The overlap is tiny -> design
    # is held.
    F = {"COMMON": 60}
    O = {"COMMON": 60}
    for i in range(500):
        F[f"Fonly{i}"] = 3
        O[f"Oonly{i}"] = 14
    r = jg.design_feasibility({"F": F, "O": O})
    assert r["design_infeasible"] is True
    # one product clears k_min in both, so it is not NO_COMMON/INSUFFICIENT; the
    # disjoint bulk makes the mixes barely overlap -> HIGH_SKEW.
    assert r["reason_code"] == "HIGH_SKEW"
    assert r["seen"]["n_common_strata_covered"] == 1
    assert r["seen"]["overlap_coefficient"] < jg.DESIGN_OVERLAP_MIN


# --- result-blind: the function has NO response parameter -------------------
def test_function_takes_no_response_argument():
    import inspect
    sig = inspect.signature(jg.design_feasibility)
    names = set(sig.parameters)
    for forbidden in ("response", "y", "outcome", "result", "response_column"):
        assert forbidden not in names, f"design_feasibility must not accept {forbidden!r}"


def test_verdict_depends_only_on_lot_structure_not_response():
    # Same lot structure judged twice; a caller cannot pass a response, so the
    # verdict is identical. This is the structural proof of result-blindness.
    gsl = {"F": {"P1": 10, "P2": 200}, "O": {"P1": 10, "P3": 200}}
    r1 = jg.design_feasibility(gsl)
    r2 = jg.design_feasibility(dict(gsl))
    assert r1 == r2


# --- the gate only ever makes things STRICTER -------------------------------
def test_thresholds_are_the_declared_constants():
    assert jg.DESIGN_K_MIN == 8
    assert jg.DESIGN_MIN_COMMON_STRATA == 1
    assert jg.DESIGN_OVERLAP_MIN == 0.5


def test_overlap_coefficient_is_symmetric_and_bounded():
    a = {"P1": 10, "P2": 30}
    b = {"P1": 20, "P2": 20}
    ov_ab = jg._overlap_coefficient(a, b)
    ov_ba = jg._overlap_coefficient(b, a)
    assert abs(ov_ab - ov_ba) < 1e-12
    assert 0.0 <= ov_ab <= 1.0


def test_seen_block_reports_what_it_looked_at():
    r = jg.design_feasibility({"F": {"P1": 10, "P2": 5}, "O": {"P1": 10, "P3": 5}})
    seen = r["seen"]
    assert set(seen) >= {
        "groups", "group_lots", "n_common_strata",
        "n_common_strata_covered", "overlap_coefficient",
        "k_min", "min_common_strata", "overlap_min",
    }
    assert seen["group_lots"]["F"] == 15
    assert seen["group_lots"]["O"] == 15
    assert seen["k_min"] == 8
