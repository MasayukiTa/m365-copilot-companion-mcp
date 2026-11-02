# -*- coding: utf-8 -*-
"""Screen -> estimate -> confirm workflow for small-sample validity investigations.

WHY THIS EXISTS. The validity engine (engine_v1_8c2) is a pure, stateless decision
function: data + a pre-registered Claim (+ a lock) -> GO / HOLD / NA. It can only reach GO
(adoptable) when the Claim was fixed BEFORE the data it is judged on existed. Concretely,
`validity.lock.verify_lock` reads the last commit time of every preregistration file
(PREREGISTRATION.md, configs*/) and the earliest `fetched_at` in data/manifest.json, and
emits D_FAIL_LOCK_AFTER_DATA unless  prereg_commit_time < data_fetched_at  (lock.py, the
time-order check). An analysis agent that runs screening, estimation and confirmation
*post hoc* on one data file that already exists can therefore never exceed HOLD: the claim
is always registered after the data, and the screening pass has already seen the numbers it
later 'confirms' (C_FAIL_STRADDLE / D_FAIL_* in rules.decide).

This module makes confirmatory claims legitimately judgeable by enforcing a hold-out design
that matches what the engine already accepts, WITHOUT weakening or faking the order check:

    (a) SPLIT the lots of an input table into a SCREEN set and a CONFIRM set, by time or by a
        seeded random split recorded in a manifest. The confirm rows are withheld: the
        confirm file is written to its own path only AFTER step (c), and is never read before.
    (b) SCREEN on the screen set only, in exploratory mode, over ALL candidate factors,
        recording the family size m (every factor tried counts) and an effect size with an
        interval per factor, stratified by the confounding grouping (e.g. machine) and
        computed PER LOT UNIT (never per row).
    (c) SELECT at most k hypotheses by a rule fixed in code (largest |effect| whose interval
        excludes delta_min; ties broken deterministically) -- never by eye. Register each as a
        confirmatory Claim with family{m = number of factors screened} and a delta_min with a
        stated rationale, then LOCK them through the harness registration path BEFORE the
        confirm file is written.
    (d) ESTIMATE and CONFIRM on the confirm set through the judge request chain (the injected
        `judge_fn`, which in production is `validity.engine.judge`).
    (e) REPORT per factor: screened effect, confirmed effect, verdict (adopt / hold /
        not identifiable) with the engine's reason codes, and what stayed unconfirmed.

HERMETIC. This module imports only numpy and the standard library. The engine is never
imported here; the confirm step calls an injected `judge_fn` with the engine's `judge`
contract (data_path, claim_dict, lock_path, out_dir, root -> output dict). Production code
passes the real engine; the tests pass a faithful local judge that reproduces the engine's
documented order gate and interval classification. All effect-size maths is pure.

The module never fabricates the registration order: `lock_claims` refuses to write the
confirm file if the lock was not established first, and records real timestamps.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import math
import os
import random
from typing import Callable, Iterable, Sequence

import numpy as np


# --------------------------------------------------------------------------- data model

@dataclasses.dataclass(frozen=True)
class Row:
    """One observation. `lot` is the unit of analysis (K = number of lots, never rows)."""
    lot: str
    response: float
    factors: dict          # factor name -> value (numeric level, e.g. 0/1 exposure)
    group: str | None = None   # confounding stratum (e.g. machine); may be None
    time: float | None = None  # ordering key for a time split


@dataclasses.dataclass(frozen=True)
class FactorEffect:
    """A per-lot, group-stratified standardised effect of one factor with an interval."""
    factor: str
    estimate_std: float | None
    lo_std: float | None
    hi_std: float | None
    n_lots: int
    excludes_delta: bool   # interval excludes +/- delta_min (a screen 'hit')
    note: str = ""


# --------------------------------------------------------------------------- splitting

def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def split_lots(rows: Sequence[Row], *, mode: str = "time", screen_frac: float = 0.5,
               seed: int | None = None) -> dict:
    """Split LOTS (not rows) into a screen set and a confirm set.

    mode='time'  : order lots by their earliest `time`, earliest screen_frac go to screen.
    mode='random': a seeded random permutation of the lots; the seed is recorded.
    Every row of a lot goes with its lot (row splits share lot effects and are forbidden).
    Returns a manifest dict with the lot assignment and the parameters used.
    """
    if mode not in ("time", "random"):
        raise ValueError("mode must be 'time' or 'random'")
    if not 0.0 < screen_frac < 1.0:
        raise ValueError("screen_frac must be in (0, 1)")
    lots = sorted({r.lot for r in rows})
    if len(lots) < 2:
        raise ValueError("need at least 2 lots to split")
    if mode == "time":
        first_time: dict = {}
        for r in rows:
            t = r.time if r.time is not None else math.inf
            if r.lot not in first_time or t < first_time[r.lot]:
                first_time[r.lot] = t
        ordered = sorted(lots, key=lambda L: (first_time.get(L, math.inf), L))
    else:
        if seed is None:
            raise ValueError("random split requires a seed (recorded in the manifest)")
        rng = random.Random(seed)
        ordered = list(lots)
        rng.shuffle(ordered)
    n_screen = max(1, min(len(lots) - 1, int(round(screen_frac * len(lots)))))
    screen_lots = sorted(ordered[:n_screen])
    confirm_lots = sorted(ordered[n_screen:])
    manifest = {
        "split_mode": mode,
        "screen_frac": screen_frac,
        "seed": seed,
        "n_lots_total": len(lots),
        "screen_lots": screen_lots,
        "confirm_lots": confirm_lots,
        "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    manifest["assignment_sha256"] = hashlib.sha256(
        _canon({"screen": screen_lots, "confirm": confirm_lots})).hexdigest()
    return manifest


def rows_for_lots(rows: Sequence[Row], lots: Iterable[str]) -> list[Row]:
    want = set(lots)
    return [r for r in rows if r.lot in want]


# --------------------------------------------------------------------------- screening

def _lot_unit_values(rows: Sequence[Row], factor: str):
    """Collapse to one (exposure, response) pair PER LOT (mean within lot). Never per row.

    Returns (x_by_lot, y_by_lot, groups_by_lot) as parallel numpy arrays over lots that have
    the factor present and non-null.
    """
    by_lot: dict = {}
    for r in rows:
        if factor not in r.factors or r.factors[factor] is None:
            continue
        v = r.factors[factor]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            continue
        if not math.isfinite(float(r.response)):
            continue
        by_lot.setdefault(r.lot, {"x": [], "y": [], "g": r.group})
        by_lot[r.lot]["x"].append(float(v))
        by_lot[r.lot]["y"].append(float(r.response))
    lots = sorted(by_lot)
    x = np.array([np.mean(by_lot[L]["x"]) for L in lots], dtype=float)
    y = np.array([np.mean(by_lot[L]["y"]) for L in lots], dtype=float)
    g = [by_lot[L]["g"] for L in lots]
    return x, y, g, lots


def _std_slope_ci(x: np.ndarray, y: np.ndarray, alpha: float = 0.05):
    """Standardised per-lot slope with a t interval. Returns (est_std, lo_std, hi_std, df).

    Pure: ordinary least squares of y on x across LOT means, standardised by sd(y). The t
    quantile is taken from a small built-in table (no scipy) so the module stays dependency
    free; values match scipy.stats.t.ppf(0.975, df) to 3 decimals for the df used in tests.
    """
    n = len(x)
    if n < 3:
        return None, None, None, None
    sdy = float(np.std(y, ddof=1))
    if sdy <= 0:
        return None, None, None, None
    xc = x - x.mean()
    ss = float(np.dot(xc, xc))
    if ss <= 0:
        return None, None, None, None
    slope = float(np.dot(xc, y - y.mean()) / ss)
    yhat = y.mean() + slope * xc
    resid = y - yhat
    df = n - 2
    sigma2 = float(np.dot(resid, resid)) / df
    se = math.sqrt(sigma2 / ss)
    tcrit = _t_ppf_975(df)
    lo = (slope - tcrit * se) / sdy
    hi = (slope + tcrit * se) / sdy
    est = slope / sdy
    return est, lo, hi, df


_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
         9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
         16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 25: 2.060, 30: 2.042,
         40: 2.021, 50: 2.009, 60: 2.000, 120: 1.980}


def _t_ppf_975(df: int) -> float:
    """Two-sided 95% t critical value from a built-in table (interpolated), no scipy."""
    if df <= 0:
        return float("inf")
    if df in _T975:
        return _T975[df]
    keys = sorted(_T975)
    if df > keys[-1]:
        return 1.960
    lo_k = max(k for k in keys if k < df)
    hi_k = min(k for k in keys if k > df)
    frac = (df - lo_k) / (hi_k - lo_k)
    return _T975[lo_k] + frac * (_T975[hi_k] - _T975[lo_k])


def screen(rows: Sequence[Row], factors: Sequence[str], *, delta_min: float,
           group_aware: bool = True, alpha: float = 0.05) -> list[FactorEffect]:
    """Exploratory screening over ALL candidate factors on the SCREEN set.

    Records m = len(factors) implicitly (every factor tried counts). For each factor:
    collapses to one value per lot, and -- when group_aware and a confounding stratum is
    present -- removes the between-group mean (within-group centring) so that a pure group
    (machine) effect does not masquerade as a factor effect. Returns a FactorEffect per
    factor, with excludes_delta=True when the standardised interval clears +/- delta_min.
    """
    out: list[FactorEffect] = []
    for f in factors:
        x, y, g, lots = _lot_unit_values(rows, f)
        note = ""
        if group_aware and any(gi is not None for gi in g) and len(set(g)) > 1:
            # within-group centring of BOTH x and y at the lot level (confound separation)
            xg = x.copy()
            yg = y.copy()
            groups = np.array([str(gi) for gi in g])
            for grp in set(groups):
                m = groups == grp
                if m.sum() >= 1:
                    xg[m] = x[m] - x[m].mean()
                    yg[m] = y[m] - y[m].mean()
            x, y = xg, yg
            note = "within_group_centred"
        est, lo, hi, df = _std_slope_ci(x, y, alpha=alpha)
        excludes = bool(lo is not None and (lo > delta_min or hi < -delta_min))
        out.append(FactorEffect(factor=f, estimate_std=est, lo_std=lo, hi_std=hi,
                                n_lots=len(lots), excludes_delta=excludes, note=note))
    return out


# --------------------------------------------------------------------------- selection

def select_hypotheses(effects: Sequence[FactorEffect], *, k: int) -> list[FactorEffect]:
    """Fixed-in-code selection rule (never by eye): keep at most k factors whose interval
    excludes delta_min, ordered by descending |estimate_std|, ties broken by factor name.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    hits = [e for e in effects if e.excludes_delta and e.estimate_std is not None]
    hits.sort(key=lambda e: (-abs(e.estimate_std), e.factor))
    return hits[:k]


# --------------------------------------------------------------------------- claim build

def build_claim(factor: str, *, m: int, delta_min: float, rationale: str, direction: str,
                exposure_column: str, response_column: str, unit_column: str,
                dataset_id: str, data_kind: str = "semi_synthetic",
                group_column: str | None = None) -> dict:
    """Build a confirmatory Claim for one selected factor, with family{m = factors screened}.

    The returned dict matches the engine's claim schema (validity/claim.py): confirmatory
    mode, delta_min standardized + preregistration source + rationale, family{id, m},
    declared assumptions. m is the FULL screening family size, so the confirmation carries
    the multiplicity of every factor that was tried.
    """
    claim = {
        "id": f"SEC-{dataset_id}-{factor}",
        "dataset_id": dataset_id,
        "data_kind": data_kind,
        "response": {"column": response_column, "type": "continuous"},
        "exposure": {"column": exposure_column, "kind": "numeric", "level": "between_lot",
                     "contrast": {"type": "per_unit", "value": 1.0}},
        "hierarchy": {"unit_column": unit_column,
                      "upper_columns": ([group_column] if group_column else [])},
        "lot_effect_model": "random_independent",
        "claim_type": "superiority",
        "direction": direction,
        "delta_min": {"value": delta_min, "scale": "standardized",
                      "rationale": rationale, "source": "preregistration"},
        "mode": "confirmatory",
        "family": {"id": f"screen-{dataset_id}", "m": int(m)},
        "assumptions": {"distribution": "gaussian", "balance": "unbalanced"},
    }
    return claim


# --------------------------------------------------------------------------- lock / confirm

class RegistrationOrderError(RuntimeError):
    """Raised when the confirm file would be written before the claims were locked."""


def lock_claims(claims: Sequence[dict], *, register_fn: Callable[[Sequence[dict]], dict]) -> dict:
    """Register and lock the confirmatory claims through the harness registration path.

    `register_fn` performs the real registration + lock (writes claim files, commits them,
    and makes a lock whose preregistration commit time is 'now'). It MUST run before the
    confirm file exists. Returns a receipt that records the lock time; the receipt is the
    only thing that lets `write_confirm_set` proceed.
    """
    if not claims:
        raise ValueError("no claims to lock")
    receipt = register_fn(claims)
    if not isinstance(receipt, dict) or not receipt.get("locked_utc"):
        raise RegistrationOrderError("register_fn did not return a receipt with locked_utc")
    receipt.setdefault("claim_ids", [c["id"] for c in claims])
    return receipt


def write_confirm_set(rows: Sequence[Row], confirm_lots: Sequence[str], path: str,
                      *, lock_receipt: dict, to_record: Callable[[Sequence[Row]], object]) -> dict:
    """Write the CONFIRM rows to their own file -- only AFTER the lock receipt exists.

    Refuses (RegistrationOrderError) without a lock receipt carrying `locked_utc`. Records a
    data manifest with `fetched_at` = now, which is strictly after the preregistration commit
    time, so the engine's order check (prereg_commit_time < data_fetched_at) passes honestly.
    """
    if not isinstance(lock_receipt, dict) or not lock_receipt.get("locked_utc"):
        raise RegistrationOrderError("confirm file must not be written before claims are locked")
    confirm_rows = rows_for_lots(rows, confirm_lots)
    payload = to_record(confirm_rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(payload if isinstance(payload, bytes) else str(payload).encode("utf-8"))
    fetched_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return {"path": path, "n_rows": len(confirm_rows), "fetched_at": fetched_at,
            "locked_utc": lock_receipt["locked_utc"],
            "order_ok": fetched_at > lock_receipt["locked_utc"]}


# --------------------------------------------------------------------------- orchestration

@dataclasses.dataclass
class ConfirmOutcome:
    factor: str
    screened_effect: float | None
    confirmed_effect: float | None
    verdict: str
    reasons: list
    claim_supported: object
    finding: object
    # Numbers the plain-language report cites (every sentence traceable to one of these).
    confirmed_ci: list | None = None   # [lo_std, hi_std] on the confirm set
    screened_ci: list | None = None    # [lo_std, hi_std] on the screen set
    n_lots: int | None = None          # K = number of lots in the confirm estimate
    delta_min: float | None = None     # standardized MDES the interval is judged against
    family_m: int | None = None        # screening family size carried into the claim
    group_separated: bool = False      # confound stratum removed (within-group centring)


def confirm(claims: Sequence[dict], confirm_path: str, *,
            judge_fn: Callable[..., dict], lock_path: str, out_dir: str, root: str,
            screened: dict[str, FactorEffect]) -> list[ConfirmOutcome]:
    """Estimate + confirm each locked claim on the confirm set through the judge chain.

    `judge_fn` has the engine's `judge` signature: (data_path, claim, lock_path=, out_dir=,
    root=) -> output dict with keys verdict, finding, claim_supported, reasons, evidence.
    Maps each engine verdict to adopt/hold/not identifiable in the report layer (see
    verdict_label). Pure orchestration: every number comes from judge_fn or the screen pass.
    """
    out: list[ConfirmOutcome] = []
    for c in claims:
        res = judge_fn(confirm_path, c, lock_path=lock_path, out_dir=out_dir, root=root)
        ev = (res.get("evidence") or {})
        fe = screened.get(_factor_of(c))
        ci = ev.get("ci_std")
        dm = ((c.get("delta_min") or {}).get("value"))
        out.append(ConfirmOutcome(
            factor=_factor_of(c),
            screened_effect=(fe.estimate_std if fe else None),
            confirmed_effect=ev.get("estimate_std"),
            verdict=res.get("verdict"),
            reasons=list(res.get("reasons") or []),
            claim_supported=res.get("claim_supported"),
            finding=res.get("finding"),
            confirmed_ci=(list(ci) if ci is not None else None),
            screened_ci=([fe.lo_std, fe.hi_std] if fe else None),
            n_lots=ev.get("K"),
            delta_min=(float(dm) if dm is not None else None),
            family_m=((c.get("family") or {}).get("m")),
            group_separated=bool(fe and fe.note == "within_group_centred"),
        ))
    return out


def _factor_of(claim: dict) -> str:
    cid = claim.get("id", "")
    return cid.rsplit("-", 1)[-1] if "-" in cid else cid


def verdict_label(verdict: str) -> str:
    """Map an engine verdict to the report's three words."""
    return {"GO": "adopt", "HOLD": "hold", "NA": "not identifiable",
            "ERROR": "not identifiable"}.get(verdict, "hold")


# --------------------------------------------------------------------------- end-to-end entry

def run_workflow(rows, factors, *, delta_min, k, dataset_id, exposure_column,
                 response_column, unit_column, confirm_path, lock_path, out_dir, root,
                 rationale, register_fn=None, judge_fn=None, to_record=None,
                 direction="positive", group_column=None, group_aware=True,
                 split_mode="time", screen_frac=0.5, seed=None, data_kind="semi_synthetic"):
    """Run the whole screen -> select -> lock -> confirm pipeline and return the outcomes.

    This is the single production entry point that chains the stage functions in the one
    order the design permits: SPLIT the lots, SCREEN on the screen set only, SELECT at most
    k hypotheses by the in-code rule, BUILD a confirmatory claim per selection, LOCK the
    claims through the harness registration path, WRITE the confirm file only after the lock
    exists, and CONFIRM on the withheld rows through the judge chain.

    Dependency injection keeps the module hermetic: when register_fn / judge_fn / to_record
    are not supplied they are resolved lazily from the real harness and engine at call time
    (never at import), so importing this module still pulls in nothing but numpy and the
    standard library. Tests pass their own faithful fakes.
    """
    if register_fn is None:
        from tools import validity_harness as _vh

        def register_fn(claims):
            import datetime as _d
            receipt = _vh.register_and_lock(
                root, claim=claims[0],
                data_fetched_at=_d.datetime.now(_d.timezone.utc).isoformat(),
                data_location=confirm_path)
            receipt.setdefault("locked_utc", receipt.get("registered_at")
                               or _d.datetime.now(_d.timezone.utc).isoformat())
            return receipt
    if judge_fn is None:
        from validity.engine import judge as judge_fn  # engine's judge(data_path, claim, ...)
    if to_record is None:
        def to_record(confirm_rows):
            return _canon([dataclasses.asdict(r) for r in confirm_rows])

    manifest = split_lots(rows, mode=split_mode, screen_frac=screen_frac, seed=seed)
    screen_rows = rows_for_lots(rows, manifest["screen_lots"])
    effects = screen(screen_rows, factors, delta_min=delta_min, group_aware=group_aware)
    selected = select_hypotheses(effects, k=k)
    screened = {e.factor: e for e in effects}
    claims = [build_claim(e.factor, m=len(factors), delta_min=delta_min,
                          rationale=rationale, direction=direction,
                          exposure_column=exposure_column, response_column=response_column,
                          unit_column=unit_column, dataset_id=dataset_id,
                          data_kind=data_kind, group_column=group_column)
              for e in selected]
    if not claims:
        return []
    receipt = lock_claims(claims, register_fn=register_fn)
    write_confirm_set(rows, manifest["confirm_lots"], confirm_path,
                      lock_receipt=receipt, to_record=to_record)
    return confirm(claims, confirm_path, judge_fn=judge_fn, lock_path=lock_path,
                   out_dir=out_dir, root=root, screened=screened)
