# -*- coding: utf-8 -*-
"""Controlled trials for the canon §5-5 evidence-rank gate (D-group / ledger-B-or-below -> HOLD).

Canon: ツール依存可能な主張一覧.md §5 item 5 (line 157):
    "根拠が D 群・台帳状態 B 以下だけの判定は、既定を保留（HOLD）にする。"
    (A verdict whose grounds are only D-group / ledger state B-or-below defaults to HOLD.)

These trials use a strong synthetic effect interval (would be GO+EFFECT on its own) and vary
ONLY the cited-evidence rank. The point under test is that the
rank gate (R_*) caps a would-be GO to HOLD, and that it is NOT the order/lock dcap (D_FAIL_*).
"""
import tools.validity_judge as vj


def _claim():
    return {
        "id": "SYN-S5R", "product": "SYN-PROD",
        "lot_effect_model": "random_independent", "exposure_level": "between_lot",
        "distribution": "gaussian", "balance": "balanced",
        "unit_column": "lot", "K": 8, "effect_type": "A", "time_unit": "day",
        "family": {"id": "F1", "m": 1}, "split_unit": "lot",
        "support": "cells", "axis0": "declared", "mode": "confirmatory", "k_min": 3,
        "delta_min": {"value": 0.5, "source": "preregistration", "rationale": "pre-declared"},
    }


def _struct():
    return {"input_errors": [], "response_constant": False, "exposure_constant": False,
            "time_gate": {}, "out_of_support": False, "balanced": True,
            "min_lots_per_level": 4}


def _strong_interval():
    # Strong positive standardized effect well above delta_min=0.5 -> classify() == EFFECT.
    return {"computable": True, "lo_std": 0.8, "hi_std": 1.5, "K": 8, "df": 6,
            "method": "between_lot_ols_on_lot_means_t_K-2"}


# --- baseline: strong effect, no dcap; opt OUT of rank gating -> GO (shows the input *would* GO)
def test_baseline_strong_effect_is_go_without_rank_gate():
    # The §5-5 rank gate now defaults fail-closed (require_evidence=True): grounds-absent -> HOLD.
    # To show the interval itself *would* GO, opt out of the gate explicitly.
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [],
                           require_evidence=False)
    assert v == "GO" and f == "EFFECT"
    # The explicit opt-out and the bare 5-arg call must agree (both opt out of the gate).
    v2, f2, r2, n2 = vj.decide(_claim(), _struct(), _strong_interval(), [], [],
                               evidence=None, require_evidence=False)
    assert (v2, f2) == (v, f)


# --- (1) D-group-only grounds -> default HOLD (R_ONLY_DIAGNOSTIC_D), finding capped to a note
def test_only_diagnostic_d_defaults_hold():
    ev = {"claims": [{"id": "D2", "ledger_state": "A"},
                     {"id": "D4", "ledger_state": "B"},
                     {"id": "D8", "ledger_state": "B"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "HOLD", "D-group-only grounds must default to HOLD, got %s" % v
    assert f is None
    assert "R_ONLY_DIAGNOSTIC_D" in r
    assert "CAPPED_FINDING_EFFECT" in n
    # It must NOT be reported via the order/lock dcap codes.
    assert not (set(r) & set(vj.D_CODES))


# --- (2) ledger-B-or-below-only grounds (even on C-ids whose state is < A) -> default HOLD
def test_only_ledger_b_or_below_defaults_hold():
    # Direct-use C ids, but all cited in ledger state B/C (not pure A) -> no upper-rank ground.
    ev = {"claims": [{"id": "C7", "ledger_state": "C"},
                     {"id": "C13", "ledger_state": "B"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "HOLD", "ledger B-or-below only must default to HOLD, got %s" % v
    assert "R_ONLY_LEDGER_B_OR_BELOW" in r
    assert not (set(r) & set(vj.D_CODES))


# --- mixed B-containing state (A-B) counts as B-or-below -> HOLD
def test_mixed_ab_state_counts_as_b_or_below():
    ev = {"claims": [{"id": "C3", "ledger_state": "A-B"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "HOLD"
    assert "R_ONLY_LEDGER_B_OR_BELOW" in r


# --- (3) a sufficient upper-rank ground (C-group, ledger state A) present -> GO preserved
def test_upper_rank_c_group_state_a_preserves_go():
    ev = {"claims": [{"id": "C1", "ledger_state": "A"},
                     {"id": "C3", "ledger_state": "A"},
                     {"id": "D4", "ledger_state": "B"}]}  # a D diagnostic may ride along
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "GO", "an A-ranked C ground must preserve GO, got %s" % v
    assert f == "EFFECT"
    assert "A_OK" in r


# --- (4a) missing evidence (grounds required but none supplied) -> HOLD, do not advance to GO
def test_missing_evidence_defaults_hold():
    for ev in ({"claims": []}, {}, {"claims": "oops"}):
        v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
        assert v == "HOLD", "missing evidence must fail safe to HOLD, got %s for %r" % (v, ev)
        assert "R_EVIDENCE_UNKNOWN" in r


# --- (4b) unknown / unparseable claim id among grounds -> HOLD (cannot certify upper rank)
def test_unknown_claim_id_defaults_hold():
    ev = {"claims": [{"id": "C3", "ledger_state": "A"}, {"id": "Z9", "ledger_state": "A"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "HOLD"
    assert "R_EVIDENCE_UNKNOWN" in r


# --- (4c) unknown ledger_state on an otherwise-C id -> treated as B-or-below (fail-safe)
def test_unknown_ledger_state_is_b_or_below():
    ev = {"claims": [{"id": "C3"}]}  # no ledger_state -> unknown -> b-or-below
    v, f, r, n = vj.decide(_claim(), _struct(), _strong_interval(), [], [], evidence=ev)
    assert v == "HOLD"
    assert "R_ONLY_LEDGER_B_OR_BELOW" in r


# --- the gate never *raises* a verdict: a straddling interval stays non-GO
def test_rank_gate_never_raises_verdict():
    # A true straddle for classify(): lo < -d and -d < hi < d -> no branch matches -> None
    # (lo_std=-0.8 < -0.5; hi_std=0.3 is within (-0.5, 0.5) but not <= -0.5, and lo<-0.5 so the
    #  practical-null branch lo>=-d fails). classify returns None -> C_FAIL_STRADDLE.
    straddle = {"computable": True, "lo_std": -0.8, "hi_std": 0.3, "K": 8, "df": 6,
                "method": "between_lot_ols_on_lot_means_t_K-2"}
    assert vj.classify(-0.8, 0.3, 0.5) is None  # guard: this really is a straddle
    # Even with a perfect upper-rank ground, a straddle cannot become GO (gate never raises).
    ev = {"claims": [{"id": "C3", "ledger_state": "A"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), straddle, [], [], evidence=ev)
    assert v == "HOLD" and "C_FAIL_STRADDLE" in r


# --- D_FAIL_* (dcap) and R_* (rank) are distinct code families: no overlap
def test_dcap_and_rank_codes_are_disjoint():
    assert not (set(vj.D_CODES) & set(vj.R_CODES))


# --- evidence_rank_gate unit behaviour (direct, no decide wrapper)
def test_evidence_rank_gate_unit():
    assert vj.evidence_rank_gate(None) == []
    assert vj.evidence_rank_gate({"claims": [{"id": "C3", "ledger_state": "A"}]}) == []
    assert "R_ONLY_DIAGNOSTIC_D" in vj.evidence_rank_gate(
        {"claims": [{"id": "D1", "ledger_state": "A-B"}]})
    assert "R_EVIDENCE_UNKNOWN" in vj.evidence_rank_gate({"claims": []})
