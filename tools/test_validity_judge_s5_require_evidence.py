# -*- coding: utf-8 -*-
"""Controlled trials for the §5-5 require_evidence gap-closure (canon section 5-5 + section 4).

Canon: ツール依存可能な主張一覧.md
    §4 (line 4): "宣言が無ければ、その主張は使えない（判定は最大でも保留...）".
    §5 item 5 (line 157): "根拠が D 群・台帳状態 B 以下だけの判定は、既定を保留（HOLD）にする。"

The c59a969c gate had a back-compatibility hole: decide() with evidence=None (the existing
5-arg call) skipped evidence_rank_gate entirely and could reach GO with NO cited ground.
That contradicts §4/§5-5 (a GO must cite an upper-rank ground). require_evidence=True closes
it: a missing/None ground on the GO path becomes R_EVIDENCE_UNKNOWN -> HOLD. The default
(require_evidence=False) preserves the pure 5-arg behaviour for callers that have not opted in.

Strong synthetic effect interval (would be GO+EFFECT on its own); only the evidence handling
varies. These synthetic rows cover the entry-to-output control table.
"""
import tools.validity_judge as vj


def _claim():
    return {
        "id": "SYN-REQ", "product": "SYN-PROD",
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


def _strong():
    return {"computable": True, "lo_std": 0.8, "hi_std": 1.5, "K": 8, "df": 6,
            "method": "between_lot_ols_on_lot_means_t_K-2"}


# --- grounds-absent now DEFAULTS to HOLD (fail-closed); explicit opt-out still reaches GO ------
def test_default_grounds_absent_is_hold_and_opt_out_still_go():
    # FIX (canon line 4 / §5-5 line 157): a GO must cite an upper-rank ground; grounds-absent is
    # at most HOLD. require_evidence now defaults to True, so the old 5-arg call (evidence=None,
    # no grounds) is capped to HOLD/R_EVIDENCE_UNKNOWN instead of silently reaching GO.
    v, f, r, n = vj.decide(_claim(), _struct(), _strong(), [], [])
    assert v == "HOLD" and f is None, "grounds-absent default must be HOLD, got %s" % v
    assert "R_EVIDENCE_UNKNOWN" in r
    assert "CAPPED_FINDING_EFFECT" in n
    # explicit evidence=None with the default (fail-closed) -> same HOLD.
    v2, f2, r2, n2 = vj.decide(_claim(), _struct(), _strong(), [], [], evidence=None)
    assert (v2, f2) == ("HOLD", None)
    # A caller may still EXPLICITLY opt out (visible at the call site) -> pure numeric GO.
    v3, f3, r3, n3 = vj.decide(_claim(), _struct(), _strong(), [], [],
                               evidence=None, require_evidence=False)
    assert (v3, f3) == ("GO", "EFFECT")


# --- opting in closes the hole: None grounds on the GO path -> HOLD, not GO -----------------
def test_require_evidence_caps_none_to_hold():
    v, f, r, n = vj.decide(_claim(), _struct(), _strong(), [], [],
                           evidence=None, require_evidence=True)
    assert v == "HOLD", "require_evidence=True must not reach GO on None grounds, got %s" % v
    assert f is None
    assert "R_EVIDENCE_UNKNOWN" in r
    assert "CAPPED_FINDING_EFFECT" in n
    # closed via the rank family, NOT via the order/lock dcap family.
    assert not (set(r) & set(vj.D_CODES))


# --- opting in does not change a verdict that already has a qualified ground ----------------
def test_require_evidence_preserves_go_with_upper_rank_ground():
    ev = {"claims": [{"id": "C1", "ledger_state": "A"}, {"id": "C3", "ledger_state": "A"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong(), [], [],
                           evidence=ev, require_evidence=True)
    assert v == "GO" and f == "EFFECT"
    assert "A_OK" in r


# --- opting in keeps the §5-5 caps for D-only / B-below grounds -----------------------------
def test_require_evidence_keeps_rank_caps():
    d_only = {"claims": [{"id": "D2", "ledger_state": "A"}, {"id": "D4", "ledger_state": "B"}]}
    v, f, r, n = vj.decide(_claim(), _struct(), _strong(), [], [],
                           evidence=d_only, require_evidence=True)
    assert v == "HOLD" and "R_ONLY_DIAGNOSTIC_D" in r
    b_below = {"claims": [{"id": "C7", "ledger_state": "C"}]}
    v2, f2, r2, n2 = vj.decide(_claim(), _struct(), _strong(), [], [],
                               evidence=b_below, require_evidence=True)
    assert v2 == "HOLD" and "R_ONLY_LEDGER_B_OR_BELOW" in r2


# --- require_evidence does not raise a verdict: a straddle stays non-GO even when opted in ---
def test_require_evidence_never_raises_a_verdict():
    straddle = {"computable": True, "lo_std": -0.8, "hi_std": 0.3, "K": 8, "df": 6,
                "method": "between_lot_ols_on_lot_means_t_K-2"}
    assert vj.classify(-0.8, 0.3, 0.5) is None
    v, f, r, n = vj.decide(_claim(), _struct(), straddle, [], [],
                           evidence=None, require_evidence=True)
    assert v == "HOLD" and "C_FAIL_STRADDLE" in r  # C cap fires before the rank gate
