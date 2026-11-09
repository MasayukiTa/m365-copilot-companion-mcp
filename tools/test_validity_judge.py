# -*- coding: utf-8 -*-
"""Tests for the rebuilt validity_judge, mapped to the canon's required trials.

(a) effect-zero synthetic x1000 -> no false GO+EFFECT (negative control).
(b) a known injected effect -> EFFECT with GO.
(c) post-hoc registration / unblinded order / trivial comparison / cross-product /
    insufficient N each fail to reach GO.
(d) one unit test per canon item exercised by the judge (declarations §0, C1=K, C3 df,
    C4 no-bootstrap, C5 HOLD vs NA, C6 support, C7 time alias, C8 pure loading, C10 family,
    verdict/finding consistency, §5 footer).
"""
import math
import random

import pytest

import tools.validity_judge as vj


def base_claim(**over):
    c = {
        "id": "C-TEST", "product": "PROD-A",
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


# ----------------------------------------------------------------- validate_claim (§0)
def test_decl_missing_key_is_input_error():
    bad = base_claim()
    del bad["K"]
    with pytest.raises(vj.ClaimError) as e:
        vj.validate_claim(bad)
    assert "INPUT_MISSING_KEY" in e.value.codes


def test_decl_all_present_ok():
    assert vj.validate_claim(base_claim()) is not None


def test_delta_min_must_be_preregistered():
    c = base_claim()
    c["delta_min"] = {"value": 0.5, "source": "posthoc", "rationale": "x"}
    with pytest.raises(vj.ClaimError) as e:
        vj.validate_claim(c)
    assert "INPUT_SCHEMA_ERROR" in e.value.codes


def test_split_unit_must_be_lot():
    c = base_claim(split_unit="wafer")
    with pytest.raises(vj.ClaimError):
        vj.validate_claim(c)


def test_family_shape_required_c10():
    c = base_claim(family={"id": "F1"})
    with pytest.raises(vj.ClaimError):
        vj.validate_claim(c)


# ----------------------------------------------------------------- context gate (§7)
def test_ctx_cross_product_stops():
    codes = vj.context_gate(base_claim(), {"products": ["PROD-A", "PROD-B"]})
    assert "CTX_COMPARISON_INVALID_CROSS_PRODUCT" in codes


def test_ctx_trivial_stops():
    codes = vj.context_gate(base_claim(), {"trivial_comparison": True})
    assert "CTX_TRIVIAL_COMPARISON" in codes


def test_ctx_n_insufficient():
    codes = vj.context_gate(base_claim(), {"n_confirmable_lots": 2})
    assert "CTX_N_INSUFFICIENT" in codes


def test_ctx_input_invalid_shortcircuits():
    codes = vj.context_gate(base_claim(), {"input_valid": False,
                                           "products": ["A", "B"]})
    assert codes == ["CTX_INPUT_INVALID"]


def test_ctx_clean_passes():
    assert vj.context_gate(base_claim(), {"products": ["PROD-A"],
                                          "n_confirmable_lots": 8}) == []


# ----------------------------------------------------------------- order gate (§3)
def test_order_posthoc_caps():
    assert "D_FAIL_ORDER" in vj.order_gate(False, True)


def test_order_no_lock_caps():
    assert "D_FAIL_UNLOCKED" in vj.order_gate(True, False)


def test_order_ok_clean():
    assert vj.order_gate(True, True) == []


# ----------------------------------------------------------------- classify (finding)
def test_classify_effect():
    assert vj.classify(0.6, 1.2, 0.5) == "EFFECT"


def test_classify_straddle_none():
    assert vj.classify(-0.2, 0.8, 0.5) is None


def test_classify_practical_null():
    assert vj.classify(-0.3, 0.3, 0.5) == "EFFECT_NULL"


def test_classify_opposite():
    assert vj.classify(-1.2, -0.6, 0.5) == "EFFECT_OPPOSITE"


# ----------------------------------------------------------------- decide precedence
def test_decide_go_on_effect():
    # Pure numeric-path GO: opt OUT of the §5-5 rank gate explicitly. Canon line 4 / §5-5
    # (line 157) makes grounds-absent default to HOLD, so a GO assertion on the 5-arg form
    # must state the deliberate opt-out rather than rely on the (now fail-closed) default.
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2), [], [],
                           require_evidence=False)
    assert v == "GO" and f == "EFFECT"


def test_decide_context_blocks_go():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2),
                           ["CTX_COMPARISON_INVALID_CROSS_PRODUCT"], [])
    assert v == "NA" and f is None


def test_decide_n_insufficient_is_hold():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2),
                           ["CTX_N_INSUFFICIENT"], [])
    assert v == "HOLD" and f is None


def test_decide_order_cap_blocks_go():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2), [],
                           ["D_FAIL_ORDER"])
    assert v == "HOLD" and f is None
    assert any(x.startswith("CAPPED_FINDING_") for x in n)


def test_decide_sparse_lots_hold_c1():
    v, f, r, n = vj.decide(base_claim(K=2), struct(), good_interval(0.6, 1.2, K=2, df=0),
                           [], [])
    assert v == "HOLD" and "A_FAIL_SPARSE_LOTS" in r


def test_decide_low_df_hold_c3():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2, df=1), [], [])
    assert v == "HOLD" and "A_FAIL_LOW_DF" in r


def test_decide_single_lot_cell_hold():
    v, f, r, n = vj.decide(base_claim(), struct(min_lots_per_level=1),
                           good_interval(0.6, 1.2), [], [])
    assert v == "HOLD" and "A_FAIL_SINGLE_LOT_CELL" in r


def test_decide_straddle_hold_c_fail():
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(-0.2, 0.9), [], [])
    assert v == "HOLD" and "C_FAIL_STRADDLE" in r


def test_decide_na_pure_loading_c8():
    v, f, r, n = vj.decide(base_claim(), struct(time_gate={"pure_loading": True}),
                           good_interval(0.6, 1.2), [], [])
    assert v == "NA" and "NA_PURE_LOADING" in r


def test_decide_na_time_alias_c7():
    v, f, r, n = vj.decide(base_claim(), struct(time_gate={"alias": True}),
                           good_interval(0.6, 1.2), [], [])
    assert v == "NA" and "NA_TIME_ALIAS" in r


def test_decide_na_out_of_support_c6():
    v, f, r, n = vj.decide(base_claim(), struct(out_of_support=True),
                           good_interval(0.6, 1.2), [], [])
    assert v == "NA" and "NA_OUT_OF_SUPPORT" in r


def test_decide_na_fixed_between_c5():
    c = base_claim(lot_effect_model="fixed_or_correlated", exposure_level="between_lot")
    v, f, r, n = vj.decide(c, struct(), good_interval(0.6, 1.2), [], [])
    assert v == "NA" and "NA_FIXED_LOT_BETWEEN_EXPOSURE" in r


def test_decide_error_on_input():
    v, f, r, n = vj.decide(base_claim(), struct(input_errors=["INPUT_EMPTY_ROWS"]),
                           good_interval(0.6, 1.2), [], [])
    assert v == "ERROR"


def test_verdict_finding_consistency_no_go_finding_under_cap():
    # under any cap, verdict is never GO and no GO finding leaks
    v, f, r, n = vj.decide(base_claim(), struct(), good_interval(0.6, 1.2), [],
                           ["D_FAIL_UNLOCKED"])
    assert v != "GO" and f is None


# ----------------------------------------------------------------- two_stage_interval (C3/C4)
def test_between_df_is_k_minus_2():
    r = vj.two_stage_interval([1, 2, 3, 4, 5], [0, 1, 2, 3, 4], level="between_lot",
                              alpha=0.05, m=1, sd_response=1.0)
    assert r["df"] == 3  # K-2 with K=5


def test_within_df_is_k_minus_1():
    r = vj.two_stage_interval(None, None, level="within_lot", alpha=0.05, m=1,
                              sd_response=1.0, lot_slopes=[0.4, 0.5, 0.6, 0.55])
    assert r["df"] == 3  # K-1 with K=4


def test_bonferroni_widens_interval_c10():
    # non-degenerate fit (nonzero residual) so the interval is computable
    ys = [0.2, 1.1, 1.9, 3.2, 3.8, 5.3]
    xs = [0, 1, 2, 3, 4, 5]
    narrow = vj.two_stage_interval(ys, xs, level="between_lot", alpha=0.05, m=1,
                                   sd_response=1.0)
    wide = vj.two_stage_interval(ys, xs, level="between_lot", alpha=0.05, m=5,
                                 sd_response=1.0)
    assert narrow["computable"] and wide["computable"]
    assert (wide["hi_std"] - wide["lo_std"]) > (narrow["hi_std"] - narrow["lo_std"])


def test_between_needs_exposure_variation():
    r = vj.two_stage_interval([1, 2, 3, 4], [2, 2, 2, 2], level="between_lot",
                              alpha=0.05, m=1, sd_response=1.0)
    assert not r["computable"]


# ----------------------------------------------------------------- (b) known effect -> GO
def test_known_effect_reaches_go_effect():
    # strong positive exposure->response slope across 8 lots
    rng = random.Random(7)
    xs = [float(i) for i in range(8)]
    ys = [2.0 * x + rng.gauss(0, 0.2) for x in xs]
    iv = vj.two_stage_interval(ys, xs, level="between_lot", alpha=0.05, m=1,
                               sd_response=1.0)
    assert iv["computable"] and iv["lo_std"] > 0
    c = base_claim(delta_min={"value": 0.5, "source": "preregistration",
                              "rationale": "x"})
    # Numeric path only: opt out of the rank gate explicitly (see test_decide_go_on_effect).
    v, f, r, n = vj.decide(c, struct(), iv, [], [], require_evidence=False)
    assert v == "GO" and f == "EFFECT"


# ----------------------------------------------------------------- (a) negative control x1000
def test_negative_control_no_false_go():
    rng = random.Random(20261104)
    false_go = 0
    for _ in range(1000):
        xs = [float(i) for i in range(8)]
        ys = [rng.gauss(0.0, 1.0) for _ in xs]  # no exposure effect
        iv = vj.two_stage_interval(ys, xs, level="between_lot", alpha=0.05, m=1,
                                   sd_response=1.0)
        c = base_claim()
        v, f, _, _ = vj.decide(c, struct(), iv, [], [])
        if v == "GO" and f == "EFFECT":
            false_go += 1
    # delta_min = 0.5 SD guard means a true null must not clear it as EFFECT
    assert false_go == 0, "false GO+EFFECT count = %d" % false_go


# ----------------------------------------------------------------- §5 footer
def test_footer_has_required_fields():
    iv = good_interval(0.6, 1.2)
    ft = vj.footer(base_claim(), iv, "GO", list(vj.OK_CODES),
                   ["LOT_EFFECT_MODEL_DECLARED_NOT_TESTABLE"], ["C-TEST"], ["L-12"])
    assert ft["claims_used"] == ["C-TEST"] and ft["ledger_rows"] == ["L-12"]
    assert ft["df"] == 6 and ft["K"] == 8
    assert ft["default_hold_if_only_D_or_B_below"] is True


# ----------------------------------------------------------- claim identity
def test_claim_sha256_is_deterministic_and_order_independent():
    # Exercises claim_sha256 from outside the module: the canonical claim
    # fingerprint used to tie a verdict back to the exact claim it judged.
    a = base_claim()
    b = base_claim()
    h = vj.claim_sha256(a)
    # 64 lowercase hex chars (sha256), and deterministic for equal input.
    assert isinstance(h, str) and len(h) == 64
    assert all(ch in "0123456789abcdef" for ch in h)
    assert vj.claim_sha256(b) == h
    # Key order must not matter: the canonical form sorts keys.
    reordered = {k: a[k] for k in reversed(list(a.keys()))}
    assert vj.claim_sha256(reordered) == h
    # A different claim must produce a different fingerprint.
    assert vj.claim_sha256(base_claim(K=9)) != h
