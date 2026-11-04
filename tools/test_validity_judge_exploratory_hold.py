# -*- coding: utf-8 -*-
"""Regression tests for the exploratory-mode HOLD cap (canon C10 / C11).

The canon (`ツール依存可能な主張一覧.md`) is explicit that exploration is at most HOLD:
  - L22  `mode`: confirmatory / exploratory (探索は最大でも保留)
  - C10 (L77-79): a family fixed BEFORE the data gives Bonferroni FWER<=alpha; a family decided
    AFTER the data does not (27 cells -> top 9 -> FWER=0.139). A scan with no declared family +
    hash is exploratory (mode=exploratory) and caps at HOLD.
  - C11 (L81-83): split-sample confirmation is valid only when discovery and confirmation sides
    are INDEPENDENT. split_unit='lot' is necessary but not sufficient; the independence of the
    two sides must itself be asserted.

Before the fix `decide()` never read claim['mode'] nor the family/split declarations, so an
exploratory claim with a strong interval reached GO+EFFECT (a false GO). These tests pin the
fixed behaviour and the required regression set (a)-(e):
  (a) confirmatory normal example reaches GO,
  (b) exploratory mode -> HOLD,
  (c) post-hoc family (m decided after data) -> HOLD,
  (d) split-independence unknown/not-asserted -> HOLD,
  (e) input missing -> ERROR.

No Windows-specific assumptions: pure Python, no path/separator/service dependencies.
"""
import pytest

import tools.validity_judge as vj


def base_claim(**over):
    c = {
        "id": "C-TEST", "product": "E-679FG(S)",
        "lot_effect_model": "random_independent", "exposure_level": "between_lot",
        "distribution": "gaussian", "balance": "balanced",
        "unit_column": "lot", "K": 8, "effect_type": "A", "time_unit": "day",
        "family": {"id": "F1", "m": 1}, "split_unit": "lot",
        "support": "cells", "axis0": "declared", "mode": "confirmatory",
        "k_min": 3,
        "delta_min": {"value": 0.5, "source": "preregistration",
                      "rationale": "pre-declared MDES"},
    }
    c.update(over)
    return c


def struct(**over):
    s = {"input_errors": [], "response_constant": False, "exposure_constant": False,
         "time_gate": {}, "out_of_support": False, "balanced": True,
         "min_lots_per_level": 4}
    s.update(over)
    return s


def good_interval(lo, hi, K=8, df=6):
    return {"computable": True, "lo_std": lo, "hi_std": hi, "K": K, "df": df,
            "method": "between_lot_ols_on_lot_means_t_K-2"}


# (a) confirmatory normal example reaches GO ---------------------------------------
def test_a_confirmatory_normal_reaches_go():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2), [], [])
    assert v == "GO" and f == "EFFECT"


# (b) exploratory mode -> HOLD (C10/C11) -------------------------------------------
def test_b_exploratory_mode_is_hold():
    v, f, r, n = vj.decide(base_claim(mode="exploratory"), struct(),
                           good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and f is None
    assert "D_FAIL_EXPLORATORY" in r
    assert "EXPLORATORY_CAP_AT_MOST_HOLD" in n


def test_b_exploratory_does_not_leak_go_finding():
    # a strong interval under the exploratory cap must not surface a GO finding; it is
    # preserved only as a CAPPED_FINDING_* note.
    v, f, r, n = vj.decide(base_claim(mode="exploratory"), struct(),
                           good_interval(0.6, 1.2), [], [])
    assert f is None
    assert any(x.startswith("CAPPED_FINDING_") for x in n)


# (c) post-hoc family -> HOLD (C10) ------------------------------------------------
def test_c_posthoc_family_preregistered_false_is_hold():
    c = base_claim(family={"id": "F-top9", "m": 9, "preregistered": False})
    v, f, r, n = vj.decide(c, struct(), good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and f is None
    assert "D_FAIL_POSTHOC_FAMILY" in r


def test_c_posthoc_family_decided_posthoc_is_hold():
    c = base_claim(family={"id": "F-top9", "m": 9, "decided": "posthoc"})
    v, f, r, n = vj.decide(c, struct(), good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and f is None
    assert "D_FAIL_POSTHOC_FAMILY" in r


# (d) split-independence not asserted -> HOLD (C11) --------------------------------
def test_d_split_independent_false_is_hold():
    v, f, r, n = vj.decide(base_claim(split_independent=False), struct(),
                           good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and f is None
    assert "D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN" in r


def test_d_split_independence_unknown_is_hold():
    v, f, r, n = vj.decide(base_claim(split_independence="unknown"), struct(),
                           good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and f is None
    assert "D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN" in r


# (e) input missing -> ERROR -------------------------------------------------------
def test_e_input_error_is_error():
    v, f, r, n = vj.decide(base_claim(), struct(input_errors=["INPUT_NO_ROWS"]),
                           good_interval(0.6, 1.2), [], [])
    assert v == "ERROR"


def test_e_missing_declaration_is_claim_error():
    bad = base_claim()
    del bad["mode"]
    with pytest.raises(vj.ClaimError) as e:
        vj.validate_claim(bad)
    assert "INPUT_MISSING_KEY" in e.value.codes


# exploratory_cap unit coverage ---------------------------------------------------
def test_exploratory_cap_clean_confirmatory_is_empty():
    assert vj.exploratory_cap(base_claim()) == []


def test_exploratory_cap_exploratory_only():
    assert vj.exploratory_cap(base_claim(mode="exploratory")) == ["D_FAIL_EXPLORATORY"]


def test_exploratory_cap_combines_independently():
    c = base_claim(mode="exploratory",
                   family={"id": "F", "m": 9, "preregistered": False},
                   split_independent=False)
    caps = set(vj.exploratory_cap(c))
    assert caps == {"D_FAIL_EXPLORATORY", "D_FAIL_POSTHOC_FAMILY",
                    "D_FAIL_SPLIT_INDEPENDENCE_UNKNOWN"}


# the cap sits below NA/ERROR in precedence --------------------------------------
def test_na_still_beats_exploratory_cap():
    # an identification stop (N/A) outranks the exploratory HOLD cap.
    c = base_claim(mode="exploratory")
    v, f, r, n = vj.decide(c, struct(out_of_support=True), good_interval(0.6, 1.2), [], [])
    assert v == "NA" and "NA_OUT_OF_SUPPORT" in r


def test_error_still_beats_exploratory_cap():
    c = base_claim(mode="exploratory")
    v, f, r, n = vj.decide(c, struct(input_errors=["INPUT_NO_ROWS"]),
                           good_interval(0.6, 1.2), [], [])
    assert v == "ERROR"
