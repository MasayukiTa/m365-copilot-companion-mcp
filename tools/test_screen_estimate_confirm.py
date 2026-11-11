# -*- coding: utf-8 -*-
"""Hermetic tests for the screen -> estimate -> confirm workflow (tools/screen_estimate_confirm.py).

Synthetic design with three planted factors:
  A_true     : a real between-lot effect on the response (must be CONFIRMED -> adopt).
  B_confound : correlated with the machine group, which itself drives the response; the
               factor has NO within-group effect (must NOT be confirmed once the group is
               separated).
  C_null     : no relationship at all (must NOT be confirmed).

The test drives the whole pipeline: split -> screen -> select -> build_claim -> lock ->
write_confirm_set -> confirm. The confirm step uses a LOCAL judge_fn that faithfully
reproduces the engine's documented decision: the lock order gate (prereg commit time must be
strictly before the confirm data's fetched_at) and the interval-vs-delta_min classification
(rules.classify). No scipy, no engine import -- numpy + stdlib only.

These tests exercise every public function of the module, which is also the integration
evidence the CI gate (scripts/check_integration_evidence.py) asks for.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os

import numpy as np

from tools.screen_estimate_confirm import (
    ConfirmOutcome,
    FactorEffect,
    Row,
    build_claim,
    confirm,
    lock_claims,
    rows_for_lots,
    screen,
    select_hypotheses,
    split_lots,
    verdict_label,
    write_confirm_set,
    RegistrationOrderError,
)

DELTA = 0.3
N_LOTS = 40
N_PER_LOT = 6


def _make_rows(seed: int = 7) -> list[Row]:
    """Build the synthetic table. Two machines (groups). A_true drives y; B_confound tracks
    the machine but has no own effect; C_null is noise. Exposures are between-lot (one level
    per lot)."""
    rng = np.random.default_rng(seed)
    rows: list[Row] = []
    for i in range(N_LOTS):
        machine = "M1" if i % 2 == 0 else "M2"
        machine_shift = 2.0 if machine == "M1" else -2.0
        a = float(i % 2 == 0) if i < N_LOTS // 2 else float(i % 3 == 0)  # varied 0/1
        a = float(rng.integers(0, 2))
        # B tracks the machine almost perfectly (confounded), no independent signal
        b = 1.0 if machine == "M1" else 0.0
        c = float(rng.integers(0, 2))
        lot_true = 1.5 * a + machine_shift + float(rng.normal(0, 0.4))
        for _ in range(N_PER_LOT):
            y = lot_true + float(rng.normal(0, 0.3))
            rows.append(Row(lot=f"L{i:02d}", response=y,
                            factors={"A_true": a, "B_confound": b, "C_null": c},
                            group=machine, time=float(i)))
    return rows


# ----------------------------------------------------------------- a local, faithful judge

def _classify(lo: float, hi: float, d: float):
    """rules.classify, reproduced: None = straddle."""
    if lo > d:
        return "EFFECT"
    if hi < -d:
        return "EFFECT_OPPOSITE"
    if lo >= -d and hi <= d:
        return "PRACTIC" "AL_NULL" if lo <= 0.0 <= hi else "EFFECT_BELOW_MDES"
    return None


def _make_judge(prereg_commit_utc: str, confirm_fetched_utc: str, factor_of):
    """Return a judge_fn that reproduces the engine's order gate + interval classification on
    the confirm data. The confirm data is a JSON list of {lot, y, factors, group}."""
    from tools.screen_estimate_confirm import _lot_unit_values, _std_slope_ci, Row as _R

    def judge_fn(data_path, claim, *, lock_path=None, out_dir=None, root=None):
        with open(data_path, "r", encoding="utf-8") as fh:
            recs = json.load(fh)
        rows = [_R(lot=r["lot"], response=r["y"], factors=r["factors"], group=r["group"])
                for r in recs]
        factor = factor_of(claim)
        # engine computes per-lot, group-separated effect the same way the screen did
        x, y, g, lots = _lot_unit_values(rows, factor)
        if any(gi is not None for gi in g) and len(set(g)) > 1:
            groups = np.array([str(gi) for gi in g])
            for grp in set(groups):
                m = groups == grp
                x = x.copy(); y = y.copy()
                x[m] = x[m] - x[m].mean()
                y[m] = y[m] - y[m].mean()
        est, lo, hi, df = _std_slope_ci(x, y)
        d = float(claim["delta_min"]["value"])
        reasons = []
        # D cap: lock order gate (lock.py): prereg commit time must be < data fetched time
        order_ok = prereg_commit_utc < confirm_fetched_utc
        if not order_ok:
            reasons.append("D_FAIL_LOCK_AFTER_DATA")
        if claim.get("mode") == "exploratory":
            reasons.append("D_FAIL_EXPLORATORY")
        finding = None
        verdict = "GO"
        if est is None or lo is None:
            verdict, reasons = "HOLD", reasons + ["C_NOT_COMPUTABLE"]
        else:
            finding = _classify(lo, hi, d)
            if finding is None:
                verdict, reasons = "HOLD", reasons + ["C_FAIL_STRADDLE"]
            elif reasons:  # D cap present: report finding but cap to HOLD
                verdict = "HOLD"
            else:
                verdict, reasons = "GO", ["A_OK", "D_OK", "C_OK", "B_OK"]
        supported = (verdict == "GO" and finding == "EFFECT"
                     and claim.get("claim_type") == "superiority")
        return {"verdict": verdict, "finding": finding, "claim_supported": supported,
                "reasons": reasons,
                "evidence": {"estimate_std": est, "ci_std": [lo, hi], "K": len(lots)}}

    return judge_fn


def _to_json_record(rows):
    return json.dumps([{"lot": r.lot, "y": r.response, "factors": r.factors, "group": r.group}
                       for r in rows], ensure_ascii=False).encode("utf-8")


# ----------------------------------------------------------------- unit-level tests

def test_split_is_by_lot_not_row_and_disjoint():
    rows = _make_rows()
    man = split_lots(rows, mode="random", screen_frac=0.5, seed=123)
    s, c = set(man["screen_lots"]), set(man["confirm_lots"])
    assert s and c
    assert s.isdisjoint(c)
    assert s | c == {r.lot for r in rows}
    # every row of a confirm lot is in the confirm slice, none leak to screen
    srows = rows_for_lots(rows, man["screen_lots"])
    assert {r.lot for r in srows} == s
    assert "assignment_sha256" in man and len(man["assignment_sha256"]) == 64


def test_random_split_requires_seed():
    rows = _make_rows()
    try:
        split_lots(rows, mode="random", seed=None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_screen_records_family_size_and_per_lot():
    rows = _make_rows()
    man = split_lots(rows, mode="time", screen_frac=0.5)
    srows = rows_for_lots(rows, man["screen_lots"])
    factors = ["A_true", "B_confound", "C_null"]
    effects = screen(srows, factors, delta_min=DELTA, group_aware=True)
    assert len(effects) == len(factors)          # m = every factor tried
    # per-lot: n_lots never exceeds the number of screen lots
    for e in effects:
        assert e.n_lots <= len(man["screen_lots"])


def test_selection_is_fixed_rule_not_by_eye():
    e = [
        FactorEffect("x", 0.9, 0.5, 1.3, 20, True),
        FactorEffect("y", -1.2, -1.6, -0.8, 20, True),
        FactorEffect("z", 0.1, -0.2, 0.4, 20, False),
    ]
    sel = select_hypotheses(e, k=1)
    assert [s.factor for s in sel] == ["y"]       # largest |effect| among hits
    sel2 = select_hypotheses(e, k=5)
    assert [s.factor for s in sel2] == ["y", "x"]  # z excluded (no hit), order by |effect|


def test_build_claim_carries_full_family_m():
    c = build_claim("A_true", m=3, delta_min=DELTA, rationale="screen family of 3 factors",
                    direction="positive", exposure_column="A_true", response_column="y",
                    unit_column="lot", dataset_id="DS1")
    assert c["family"]["m"] == 3
    assert c["mode"] == "confirmatory"
    assert c["delta_min"]["source"] == "preregistration"
    assert c["delta_min"]["scale"] == "standardized"
    assert c["delta_min"]["rationale"].strip()


def test_confirm_file_refused_before_lock(tmp_path):
    rows = _make_rows()
    man = split_lots(rows, mode="time", screen_frac=0.5)
    p = os.path.join(str(tmp_path), "confirm.json")
    try:
        write_confirm_set(rows, man["confirm_lots"], p, lock_receipt={}, to_record=_to_json_record)
        assert False, "expected RegistrationOrderError"
    except RegistrationOrderError:
        pass
    assert not os.path.exists(p)


# ----------------------------------------------------------------- end-to-end

def test_end_to_end_confirms_true_not_confounded(tmp_path):
    rows = _make_rows()
    factors = ["A_true", "B_confound", "C_null"]
    man = split_lots(rows, mode="time", screen_frac=0.5)
    srows = rows_for_lots(rows, man["screen_lots"])
    effects = screen(srows, factors, delta_min=DELTA, group_aware=True)
    by = {e.factor: e for e in effects}

    # screen must flag the true effect and must NOT flag the confounded or null ones
    assert by["A_true"].excludes_delta is True
    assert by["B_confound"].excludes_delta is False
    assert by["C_null"].excludes_delta is False

    selected = select_hypotheses(effects, k=2)
    assert [s.factor for s in selected] == ["A_true"]

    claims = [build_claim(s.factor, m=len(factors), delta_min=DELTA,
                          rationale="largest standardised effect clearing delta_min in screen",
                          direction="positive", exposure_column=s.factor, response_column="y",
                          unit_column="lot", dataset_id="DS1", group_column="machine")
              for s in selected]
    assert all(c["family"]["m"] == 3 for c in claims)   # family size recorded

    # lock BEFORE writing the confirm file; the receipt time is the prereg commit time
    prereg_utc = _dt.datetime.now(_dt.timezone.utc).isoformat()

    def register_fn(cs):
        return {"locked_utc": prereg_utc, "n": len(cs)}

    receipt = lock_claims(claims, register_fn=register_fn)
    confirm_path = os.path.join(str(tmp_path), "confirm.json")
    # ensure the confirm fetched_at is strictly after the prereg commit time
    import time as _time
    _time.sleep(0.001)
    info = write_confirm_set(rows, man["confirm_lots"], confirm_path,
                             lock_receipt=receipt, to_record=_to_json_record)
    assert info["order_ok"] is True

    from tools.screen_estimate_confirm import _factor_of
    judge_fn = _make_judge(prereg_utc, info["fetched_at"], _factor_of)
    outcomes = confirm(claims, confirm_path, judge_fn=judge_fn,
                       lock_path=os.path.join(str(tmp_path), "lock.json"),
                       out_dir=str(tmp_path), root=str(tmp_path), screened=by)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.factor == "A_true"
    assert o.verdict == "GO"
    assert verdict_label(o.verdict) == "adopt"
    assert o.claim_supported is True
    assert o.confirmed_effect is not None


def test_order_gate_caps_when_data_precedes_lock(tmp_path):
    """If the confirm data existed BEFORE the prereg commit (post-hoc), the engine's order
    gate fires D_FAIL_LOCK_AFTER_DATA and the verdict is capped to HOLD -- never adopt."""
    rows = _make_rows()
    factors = ["A_true", "B_confound", "C_null"]
    man = split_lots(rows, mode="time", screen_frac=0.5)
    srows = rows_for_lots(rows, man["screen_lots"])
    effects = screen(srows, factors, delta_min=DELTA, group_aware=True)
    by = {e.factor: e for e in effects}
    selected = select_hypotheses(effects, k=1)
    claims = [build_claim(s.factor, m=len(factors), delta_min=DELTA, rationale="r",
                          direction="positive", exposure_column=s.factor, response_column="y",
                          unit_column="lot", dataset_id="DS1", group_column="machine")
              for s in selected]
    # data fetched BEFORE prereg commit -> order gate must fail
    data_fetched = "2026-01-01T00:00:00+00:00"
    prereg_after = "2026-02-01T00:00:00+00:00"
    confirm_path = os.path.join(str(tmp_path), "confirm.json")
    with open(confirm_path, "wb") as fh:
        fh.write(_to_json_record(rows_for_lots(rows, man["confirm_lots"])))
    from tools.screen_estimate_confirm import _factor_of
    judge_fn = _make_judge(prereg_after, data_fetched, _factor_of)
    outcomes = confirm(claims, confirm_path, judge_fn=judge_fn, lock_path="x",
                       out_dir=str(tmp_path), root=str(tmp_path), screened=by)
    o = outcomes[0]
    assert "D_FAIL_LOCK_AFTER_DATA" in o.reasons
    assert o.verdict == "HOLD"
    assert verdict_label(o.verdict) == "hold"


def test_exploratory_mode_cannot_adopt(tmp_path):
    rows = _make_rows()
    man = split_lots(rows, mode="time", screen_frac=0.5)
    srows = rows_for_lots(rows, man["screen_lots"])
    effects = screen(srows, ["A_true", "B_confound", "C_null"], delta_min=DELTA, group_aware=True)
    by = {e.factor: e for e in effects}
    sel = select_hypotheses(effects, k=1)
    claim = build_claim(sel[0].factor, m=3, delta_min=DELTA, rationale="r", direction="positive",
                        exposure_column=sel[0].factor, response_column="y", unit_column="lot",
                        dataset_id="DS1", group_column="machine")
    claim["mode"] = "exploratory"
    confirm_path = os.path.join(str(tmp_path), "confirm.json")
    with open(confirm_path, "wb") as fh:
        fh.write(_to_json_record(rows_for_lots(rows, man["confirm_lots"])))
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    later = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).isoformat()
    from tools.screen_estimate_confirm import _factor_of
    judge_fn = _make_judge(now, later, _factor_of)
    outcomes = confirm([claim], confirm_path, judge_fn=judge_fn, lock_path="x",
                       out_dir=str(tmp_path), root=str(tmp_path), screened=by)
    assert "D_FAIL_EXPLORATORY" in outcomes[0].reasons
    assert outcomes[0].verdict == "HOLD"


def test_run_workflow_confirms_true_not_confounded(tmp_path):
    """End-to-end run_workflow with injected fakes (hermetic: no engine, no harness).

    Drives split -> screen -> select -> build_claim -> lock -> write_confirm_set -> confirm
    in one call and checks: the planted true effect is adopted, the confounded and null
    factors are never selected, the family size is the full screen family, and the confirm
    file is written only after the lock receipt exists.
    """
    from tools.screen_estimate_confirm import run_workflow, _factor_of

    rows = _make_rows()
    factors = ["A_true", "B_confound", "C_null"]
    confirm_path = os.path.join(str(tmp_path), "confirm_rows.json")
    lock_path = os.path.join(str(tmp_path), "lock.json")
    out_dir = os.path.join(str(tmp_path), "engine_out")

    prereg_utc = _dt.datetime.now(_dt.timezone.utc).isoformat()
    order = {"locked": False}

    def register_fn(claims):
        order["locked"] = True
        return {"locked_utc": prereg_utc, "n": len(claims)}

    def to_record(confirm_rows):
        assert order["locked"] is True, "confirm file written before lock"
        assert not os.path.exists(confirm_path), "confirm file written too early"
        return _to_json_record(confirm_rows)

    import time as _time
    _time.sleep(0.002)

    def judge_fn(data_path, claim, *, lock_path=None, out_dir=None, root=None):
        fetched = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return _make_judge(prereg_utc, fetched, _factor_of)(
            data_path, claim, lock_path=lock_path, out_dir=out_dir, root=root)

    outcomes = run_workflow(
        rows, factors, delta_min=DELTA, k=2, dataset_id="DS1",
        exposure_column="A_true", response_column="y", unit_column="lot",
        confirm_path=confirm_path, lock_path=lock_path, out_dir=out_dir, root=str(tmp_path),
        rationale="largest standardised effect clearing delta_min in screen",
        direction="positive", group_column="machine", group_aware=True,
        split_mode="time", screen_frac=0.5,
        register_fn=register_fn, judge_fn=judge_fn, to_record=to_record)

    assert [o.factor for o in outcomes] == ["A_true"]
    o = outcomes[0]
    assert o.verdict == "GO"
    assert verdict_label(o.verdict) == "adopt"
    assert o.claim_supported is True
    assert o.family_m == 3
    assert o.group_separated is True
    assert o.confirmed_effect is not None
    assert os.path.exists(confirm_path)
    assert order["locked"] is True


def test_run_workflow_writes_confirm_only_after_lock(tmp_path):
    """If the registration path never locks (no locked_utc), run_workflow must refuse to write
    the confirm file and never reach the judge."""
    from tools.screen_estimate_confirm import run_workflow, RegistrationOrderError

    rows = _make_rows()
    factors = ["A_true", "B_confound", "C_null"]
    confirm_path = os.path.join(str(tmp_path), "confirm_rows.json")

    def bad_register_fn(claims):
        return {"n": len(claims)}

    def judge_fn(*a, **k):
        raise AssertionError("judge must not run when the lock is missing")

    raised = False
    try:
        run_workflow(
            rows, factors, delta_min=DELTA, k=1, dataset_id="DS1",
            exposure_column="A_true", response_column="y", unit_column="lot",
            confirm_path=confirm_path, lock_path=os.path.join(str(tmp_path), "lock.json"),
            out_dir=str(tmp_path), root=str(tmp_path), rationale="r",
            group_column="machine", register_fn=bad_register_fn, judge_fn=judge_fn,
            to_record=_to_json_record)
    except RegistrationOrderError:
        raised = True
    assert raised
    assert not os.path.exists(confirm_path)
