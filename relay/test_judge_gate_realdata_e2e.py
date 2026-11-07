# -*- coding: utf-8 -*-
"""End-to-end tests for the confirmation-data independence screen (fixed condition 2),
driven by a REAL-DATA fixture distilled from the ロス (mcl_loss_ftype_confirm) run, and a
synthetic INDEPENDENT fixture that a GO must survive.

WHY THIS FILE EXISTS (owner task 2026-11-03, this conversation's slice = 6/7, end-to-end).
The unit tests in relay/test_judge_gate_harness.py pin confirm_overlap_fraction /
apply_confirm_independence / confirm_independence_note on small synthetic inputs. They do NOT
exercise the whole chain on the actual ロス confirmation slice, where the gate must STOP a GO
that only looked ordered because the registration clock preceded the data-fetch clock. These
tests run the chain end to end:

  (A) REAL-DATA (opened-before-registration -> HOLD/保留). A fixture distilled from the live
      run: confirmation lots taken verbatim from 生データ_後半_確認用.csv, each of which the
      data-access ledger shows was opened by the prior full-period analysis `mcl_loss`
      (ledger data_mtime 2026-11-03T08:30:44+09:00) BEFORE this confirmation claim's trusted
      registration time (2026-11-03T10:36:39+09:00, the order_source in judge_result.json).
      The chain must: compute overlap fraction > 0, FORCE order_ok true->false, and put
      「確認用データは開封済み(独立でない)」 on the footer. The engine's own GO is preserved
      on the verdict object (we never edit it); the harness/footer is what shows 保留.

  (B) SYNTHETIC INDEPENDENT (fetched-after-registration -> GO stays GO). Every confirmation lot
      was opened AFTER registration (or never): overlap fraction == 0, order_ok stays true, and
      no independence line appears -- a genuinely independent confirmation must not be blocked.

NO SECRET DATA. The lot identifiers below are manufacturing lot numbers already present in the
repo-external raw CSV; they carry NO measurement value and NO employee id. The fixture is the
MINIMUM needed to reproduce the behaviour, inlined so the test is hermetic (it reads no file
under C:/Users/... and runs on the Linux CI runner). The full-slice numbers measured on the
real files in this run (confirm lots 8682; direct ledger overlap 4748 = 54.7%) are recorded in
the PR description and the electronic lab note, not asserted here, because the raw files are not
shipped to CI.

Stricter/add-information only: every assertion checks the gate STOPS (order_ok true->false) or
ADDS a finding; none checks it relaxing anything. The engine's thresholds/claims/delta_min are
never touched.
"""
from __future__ import annotations

import pytest

jg = pytest.importorskip(
    "relay.judge_gate",
    reason="judge_gate not importable",
)
from relay.judge_gate import (  # noqa: E402
    confirm_overlap_fraction,
    apply_confirm_independence,
    confirm_independence_note,
    build_judge_footer,
)

# --- REAL-DATA FIXTURE (distilled from mcl_loss_ftype_confirm in this run) ------------------
#
# This claim's TRUSTED registration time, read from judge_result.json's order_source
# ("registration.registered_at (2026-11-03T10:36:39...) precedes data fetched_at ..."). It is
# what the engine used to call order_ok=true on a clock-order basis alone.
_LOSS_REGISTERED_AT = "2026-11-03T10:36:39.215472+09:00"

# The time the prior full-period analysis `mcl_loss` opened its lots: the data-access ledger
# records its data_mtime as 2026-11-03T08:30:44+09:00 -- BEFORE the registration above. So any
# confirmation lot that `mcl_loss` opened is a pre-registration leak.
_MCL_LOSS_OPENED_AT = "2026-11-03T08:30:44.838340+09:00"

# Confirmation lots taken verbatim from 生データ_後半_確認用.csv whose lot number is present in
# the `mcl_loss` ledger lot set (n_lots=30324). Keyed by (exposure, response) per the gate's
# contract; here exposure is 設備区分, response is 不良量合計 -- the key is
# "<exposure>|<lot>" so lots of the same lot number under different exposure groups are distinct.
_REAL_CONFIRM_OPENED = [
    ["その他設備", "不良量合計", "EUH1A25-1-1"],
    ["その他設備", "不良量合計", "EUH1A25-1-2"],
    ["その他設備", "不良量合計", "EUH1A25-3-1"],
    ["その他設備", "不良量合計", "EUH1A25-3-2"],
    ["その他設備", "不良量合計", "EUH1B28-2-2"],
    ["FtypePP外観検査機", "不良量合計", "EV01509"],
]

# The ledger of lots a prior analysis opened and when: every real confirmation lot above was
# opened by `mcl_loss` at _MCL_LOSS_OPENED_AT (before registration). Keyed the same way.
_REAL_PRIOR_OPENED = {
    "|".join([exp, lot]): _MCL_LOSS_OPENED_AT
    for exp, _resp, lot in _REAL_CONFIRM_OPENED
}


def _confirm_keys(triples):
    """Normalise [exposure, response, lot] triples to the gate's (exposure, lot) key used in
    the prior_opened mapping above -- the same key the mapping is built from."""
    return [[exp, lot] for exp, _resp, lot in triples]


# --- (A) REAL-DATA: opened-before-registration -> order_ok forced false, footer says so -----

def test_real_loss_confirmation_slice_is_detected_opened_and_order_forced_false():
    """The ロス confirmation lots were all opened by `mcl_loss` before this claim's registration;
    the chain must detect overlap > 0 and force order_ok true->false."""
    confirm = _confirm_keys(_REAL_CONFIRM_OPENED)
    overlap = confirm_overlap_fraction(
        confirm, _REAL_PRIOR_OPENED, registered_at=_LOSS_REGISTERED_AT)
    # every real confirmation lot was opened before registration -> fraction 1.0 here
    assert overlap["n_lots"] == len(confirm)
    assert overlap["n_overlap"] == len(confirm)
    assert overlap["fraction"] == pytest.approx(1.0)

    # engine returned a clock-order GO; the harness order_record starts order_ok True
    order = {
        "order_ok": True,
        "registered_at": _LOSS_REGISTERED_AT,
        "data_fetched_at": "2026-11-03T10:38:28.483855+09:00",
        "order_reason": "registration precedes data fetch (clock order)",
    }
    apply_confirm_independence(order, overlap)
    assert order["order_ok"] is False  # stricter: true forced to false
    rec = order["confirm_independence"]
    assert rec["checked"] is True
    assert rec["independent"] is False
    assert rec["opened_before_registration_fraction"] == pytest.approx(1.0)
    assert "NOT independent" in order["order_reason"]


def test_real_loss_footer_states_confirmation_opened_and_hold():
    """The final answer the owner reads must carry order_ok=False and the plain-Japanese
    「確認用データは開封済み(独立でない)」 line -- never a silent GO. The engine's own GO is
    preserved on the verdict object unchanged; the footer is what shows 保留."""
    confirm = _confirm_keys(_REAL_CONFIRM_OPENED)
    overlap = confirm_overlap_fraction(
        confirm, _REAL_PRIOR_OPENED, registered_at=_LOSS_REGISTERED_AT)
    order = {
        "order_ok": True,
        "registered_at": _LOSS_REGISTERED_AT,
        "data_fetched_at": "2026-11-03T10:38:28.483855+09:00",
    }
    apply_confirm_independence(order, overlap)
    # The engine's own output is kept verbatim (GO/EFFECT/order_ok true), NOT edited:
    verdict = {
        "id": "mcl_loss_ftype_confirm", "verdict": "GO", "verdict_ja": "採用",
        "finding": "EFFECT", "claim_supported": True,
        "reasons": ["A_OK", "D_OK", "C_OK", "B_OK"], "notes": [],
        "evidence": {"K": 8682, "ci_std": [1.3879, 1.6478]},
        "hashes": {"input_sha256": "8" * 64},
    }
    footer = build_judge_footer(
        verdict=verdict, engine_available=True, order_record=order)
    # harness-level order is false and the reason is stated plainly
    assert "order_ok=False" in footer
    assert "確認用データは開封済み" in footer
    assert "独立でない" in footer
    # the engine's own verdict object is untouched (still GO) -- we read it, never edit it
    assert verdict["verdict"] == "GO"
    assert confirm_independence_note(order) != ""


def test_real_loss_partial_overlap_still_stops_when_fraction_positive():
    """Even a SINGLE opened-before-registration lot among otherwise-fresh ones makes the slice
    non-independent (fraction > 0) and stops the GO. Mirrors the real slice where 54.7% of the
    8682 confirmation lots were in the `mcl_loss` ledger -- any positive share suffices."""
    confirm = [
        ["その他設備", "EUH1A25-1-1"],   # opened before registration (leak)
        ["その他設備", "FRESH-0001"],      # never opened by a prior analysis
        ["その他設備", "FRESH-0002"],
        ["その他設備", "FRESH-0003"],
    ]
    prior = {"その他設備|EUH1A25-1-1": _MCL_LOSS_OPENED_AT}
    overlap = confirm_overlap_fraction(confirm, prior, registered_at=_LOSS_REGISTERED_AT)
    assert overlap["n_overlap"] == 1
    assert overlap["fraction"] == pytest.approx(0.25)
    order = {"order_ok": True, "registered_at": _LOSS_REGISTERED_AT}
    apply_confirm_independence(order, overlap)
    assert order["order_ok"] is False  # any positive share stops it


# --- (B) SYNTHETIC INDEPENDENT: fetched-after-registration -> GO stays GO -------------------

def test_synthetic_independent_confirmation_keeps_go():
    """A genuinely independent confirmation -- every lot opened AFTER registration (or never) --
    must NOT be blocked: overlap fraction 0, order_ok stays true, no independence line, GO
    survives end to end."""
    reg = "2026-11-02T20:56:46+09:00"
    confirm = [
        ["その他設備", "NEW-0001"],
        ["その他設備", "NEW-0002"],
        ["FtypePP外観検査機", "NEW-0003"],
    ]
    # one lot WAS opened, but strictly AFTER registration -> not a pre-registration leak;
    # the others were never opened by any prior analysis.
    prior = {"その他設備|NEW-0001": "2026-11-03T00:00:00+09:00"}
    overlap = confirm_overlap_fraction(confirm, prior, registered_at=reg)
    assert overlap["fraction"] == 0.0
    assert overlap["n_overlap"] == 0

    order = {
        "order_ok": True, "registered_at": reg,
        "data_fetched_at": "2026-11-02T21:00:00+09:00",
    }
    apply_confirm_independence(order, overlap)
    assert order["order_ok"] is True  # independent: unchanged
    assert order["confirm_independence"]["independent"] is True

    verdict = {
        "id": "SYN_INDEP", "verdict": "GO", "verdict_ja": "採用",
        "finding": "EFFECT", "claim_supported": True,
        "reasons": [], "notes": [], "evidence": {"K": 12},
        "hashes": {"input_sha256": "1" * 64},
    }
    footer = build_judge_footer(
        verdict=verdict, engine_available=True, order_record=order)
    assert "order_ok=True" in footer
    assert "確認用データは開封済み" not in footer
    assert "GO" in footer and "採用" in footer
    assert confirm_independence_note(order) == ""


def test_synthetic_independent_empty_confirmation_is_independent():
    """No confirmation lots at all -> nothing to leak -> fraction 0, order_ok unchanged."""
    reg = "2026-11-02T20:56:46+09:00"
    overlap = confirm_overlap_fraction([], {}, registered_at=reg)
    assert overlap == {"fraction": 0.0, "n_lots": 0, "n_overlap": 0, "overlap_lots": []}
    order = {"order_ok": True, "registered_at": reg}
    apply_confirm_independence(order, overlap)
    assert order["order_ok"] is True
    assert order["confirm_independence"]["independent"] is True
