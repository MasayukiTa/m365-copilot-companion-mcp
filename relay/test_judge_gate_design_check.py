# -*- coding: utf-8 -*-
"""Tests for the design-check fields added to the validity-judge footer / judge_result.json.

WHAT THIS PINS. relay/judge_gate.py now reads an optional `design_check` section of a worker's
judge request and ADDS two facts to the judge footer and to judge_result.json, under a single
`design_check` key:

  (A) stratifier presence -- whether the analysis declared at least one stratifier / blocking
      column (層別列の有無);
  (B) per-exposure-group product skew -- for each exposure group, the dominant 品名 and its
      share, plus a flag that is true only when every exposure group drew from the IDENTICAL set
      of products. The loss confirmation (時間分割確認_20261103) failed exactly this: it returned
      GO without holding the 塗工布品名 fixed across the exposure groups.

These fields only ADD INFORMATION. They never change the engine verdict and never loosen
order_ok -- those belong to other parts of the harness. All fixtures here are synthetic: no
measurement row, no employee id, only column names / product labels / counts.
"""
from __future__ import annotations

from relay import judge_gate as J


# ───────────────────────── validation of the design_check section

def test_a_missing_section_is_allowed_not_an_error():
    # The section itself is optional; _validate_design_check is only called when present, but it
    # must accept a well-formed empty object as usable (no fields is allowed).
    assert J._validate_design_check({}) == ""


def test_a_non_object_section_is_named():
    assert "design_check must be an object" in J._validate_design_check([1, 2, 3])


def test_stratifier_columns_must_be_a_list_of_strings():
    assert "stratifier_columns" in J._validate_design_check({"stratifier_columns": "lot"})
    assert "stratifier_columns" in J._validate_design_check({"stratifier_columns": [1]})
    assert J._validate_design_check({"stratifier_columns": ["lot", "product"]}) == ""


def test_exposure_groups_must_map_group_to_product_counts():
    assert "exposure_groups" in J._validate_design_check({"exposure_groups": ["A", "B"]})
    assert "exposure_groups" in J._validate_design_check(
        {"exposure_groups": {"A": "not-an-object"}})
    assert "non-negative" in J._validate_design_check(
        {"exposure_groups": {"A": {"P1": -1}}})
    assert "non-negative" in J._validate_design_check(
        {"exposure_groups": {"A": {"P1": True}}})  # bool is not a count
    assert J._validate_design_check(
        {"exposure_groups": {"A": {"P1": 3, "P2": 1}}}) == ""


# ───────────────────────── computed result: stratifier presence

def test_stratifier_presence_is_reported():
    r = J.design_check_result({"stratifier_columns": ["product", "line"]})
    assert r["stratifier_present"] is True
    assert r["stratifier_columns"] == ["product", "line"]


def test_no_stratifier_column_is_reported_as_absent():
    r = J.design_check_result({"exposure_groups": {"A": {"P1": 2}}})
    assert r["stratifier_present"] is False
    assert r["stratifier_columns"] == []


# ───────────────────────── computed result: product skew

def test_aligned_products_across_groups_is_not_skew():
    # Both exposure groups drew from the SAME product set -> aligned, no skew.
    r = J.design_check_result({"exposure_groups": {
        "high": {"P1": 5, "P2": 5},
        "low": {"P1": 4, "P2": 6},
    }})
    skew = r["product_skew"]
    assert skew["products_aligned"] is True
    assert skew["skew_detected"] is False


def test_different_product_sets_across_groups_is_skew():
    # This is the loss-confirmation shape: each exposure group is a different product.
    r = J.design_check_result({"exposure_groups": {
        "high": {"PA": 10},
        "low": {"PB": 10},
    }})
    skew = r["product_skew"]
    assert skew["products_aligned"] is False
    assert skew["skew_detected"] is True
    assert skew["per_group"]["high"]["dominant_product"] == "PA"
    assert skew["per_group"]["high"]["dominant_share"] == 1.0
    assert skew["per_group"]["low"]["dominant_product"] == "PB"


def test_dominant_share_is_a_fraction_of_the_group_total():
    r = J.design_check_result({"exposure_groups": {"A": {"P1": 3, "P2": 1}}})
    g = r["product_skew"]["per_group"]["A"]
    assert g["total"] == 4
    assert g["dominant_product"] == "P1"
    assert g["dominant_share"] == 0.75


def test_no_exposure_groups_leaves_skew_flags_none():
    r = J.design_check_result({"stratifier_columns": ["product"]})
    assert r["product_skew"]["products_aligned"] is None
    assert r["product_skew"]["skew_detected"] is None


# ───────────────────────── footer rendering

def test_footer_is_empty_without_a_result():
    assert J.format_design_check_footer(None) == []
    assert J.format_design_check_footer({}) == []


def test_footer_names_stratifier_and_skew():
    r = J.design_check_result({
        "stratifier_columns": ["product"],
        "exposure_groups": {"high": {"PA": 10}, "low": {"PB": 10}},
    })
    lines = J.format_design_check_footer(r)
    text = "\n".join(lines)
    assert "設計検査" in text
    assert "層別列: product" in text
    assert "不一致" in text  # skew surfaced for human review


def test_footer_says_none_when_no_stratifier():
    r = J.design_check_result({"exposure_groups": {"A": {"P1": 1}}})
    text = "\n".join(J.format_design_check_footer(r))
    assert "層別列: なし" in text


# ───────────────────────── wiring into build_judge_footer (both branches)

def _verdict():
    return {"id": "c1", "verdict": "ADOPT", "verdict_ja": "採用", "finding": "f",
            "claim_supported": True, "reasons": [], "notes": [], "evidence": {},
            "hashes": {}}


def test_design_check_appears_when_engine_ran():
    r = J.design_check_result({"exposure_groups": {"high": {"PA": 2}, "low": {"PB": 2}}})
    footer = J.build_judge_footer(verdict=_verdict(), engine_available=True,
                                  design_check=r)
    assert "設計検査" in footer
    assert "不一致" in footer


def test_design_check_appears_even_when_engine_did_not_run():
    r = J.design_check_result({"exposure_groups": {"high": {"PA": 2}, "low": {"PB": 2}}})
    footer = J.build_judge_footer(verdict=None, engine_available=False,
                                  unavailable_reason="engine root not found",
                                  design_check=r)
    assert "judge NOT run" in footer
    assert "設計検査" in footer  # information is added regardless of the engine state


def test_footer_unchanged_when_no_design_check_supplied():
    # Backward compatibility: without a design_check arg the footer carries no design block.
    footer = J.build_judge_footer(verdict=_verdict(), engine_available=True)
    assert "設計検査" not in footer
