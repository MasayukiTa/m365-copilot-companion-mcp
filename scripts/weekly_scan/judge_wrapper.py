# -*- coding: utf-8 -*-
"""Read-only wrapper over an already-produced judge result.

WHAT THIS IS, AND IS NOT. The weekly scan must never move a verdict. The judge, its
rules, the thresholds, Delta-min, the correction, and the order / trusted-registration
clock are all OWNED by the existing validity engine and its registered artifacts. This
module only READS the files that engine already wrote (``judge_result.json``, and the
sibling ``manifest.json`` / ``engine_claim_*.json`` when present) and exposes their
values as a small, typed view. It:

* never runs the judge,
* never recomputes a verdict,
* never writes or edits any judge artifact,
* never fabricates or back-dates an order / registration time.

If a later scan step needs a FRESH verdict (because new confirmation lots arrived), it
must ask the real engine to judge a newly-registered claim -- NOT this module. This
module's job is to tell the scan what the registered result already says, verbatim.

The field layout mirrors the real ``judge_result.json`` seen in the operator's
``\u5224\u5b9a\u5668\u306e\u5b9f\u7269`` folders: top-level ``verdict`` is itself an object with
``verdict`` (HOLD/GO/...), ``reasons``, ``evidence`` (K, df, ci_std, estimate_std,
delta_min_std, loo_n), and ``diagnostics.capacity`` (k_required_p80/p90, additional_
lots_p80/p90, k_min, ...). The top-level ``order`` object carries order_ok and the
recorded registration / data times.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class JudgeReadError(ValueError):
    """A judge artifact is missing or mis-shaped. Raised rather than guessed, so the
    scan never treats an unreadable result as 'no effect'."""


@dataclass(frozen=True)
class OrderRecord:
    """The recorded registration-vs-data order, read verbatim. Never recomputed."""

    order_ok: Optional[bool]
    registered_at: Optional[str]
    registered_at_trusted: Optional[bool]
    data_fetched_at: Optional[str]
    data_file_mtime: Optional[str]
    lock_recorded: Optional[bool]
    order_reason: Optional[str]


@dataclass(frozen=True)
class Capacity:
    """The lot-capacity diagnostics: how many lots the registered result says are
    needed, and how many more. Read verbatim from diagnostics.capacity."""

    K: Optional[int]
    k_min: Optional[int]
    k_min_shortfall: Optional[int]
    k_required_p80: Optional[int]
    k_required_p90: Optional[int]
    additional_lots_p80: Optional[int]
    additional_lots_p90: Optional[int]
    computable: Optional[bool]


@dataclass(frozen=True)
class JudgeResult:
    """A small typed view over one judge_result.json. All values are read as written."""

    id: Optional[str]
    verdict: Optional[str]
    verdict_ja: Optional[str]
    reasons: tuple[str, ...]
    rules_version: Optional[str]
    # evidence
    K: Optional[int]
    df: Optional[int]
    loo_n: Optional[int]
    estimate_std: Optional[float]
    ci_std: Optional[tuple[float, float]]
    delta_min_std: Optional[float]
    # derived read-only flags
    order: Optional[OrderRecord]
    capacity: Optional[Capacity]
    #: The raw parsed JSON, so a caller that needs a field not surfaced here can read
    #: it without re-opening the file -- still read-only.
    raw: dict[str, Any]

    @property
    def interval_inside_threshold(self) -> Optional[bool]:
        """True when the standardized interval lies entirely within +/- delta_min
        (i.e. 'no effect' territory). None when the interval or delta_min is missing.

        This is a READ of the already-registered numbers, used only to help the scan
        CLASSIFY a miss (A vs the rest). It does not change any verdict.
        """
        if self.ci_std is None or self.delta_min_std is None:
            return None
        lo, hi = self.ci_std
        d = abs(self.delta_min_std)
        return (lo >= -d) and (hi <= d)

    @property
    def interval_excludes_zero(self) -> Optional[bool]:
        """True when the standardized interval does not contain 0. None if missing."""
        if self.ci_std is None:
            return None
        lo, hi = self.ci_std
        return (lo > 0) or (hi < 0)


def _as_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None if isinstance(v, bool) else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_pair(v: Any) -> Optional[tuple[float, float]]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return None
    lo, hi = _as_float(v[0]), _as_float(v[1])
    if lo is None or hi is None:
        return None
    return (lo, hi)


def _read_order(obj: dict[str, Any]) -> Optional[OrderRecord]:
    o = obj.get("order")
    if not isinstance(o, dict):
        return None
    return OrderRecord(
        order_ok=o.get("order_ok") if isinstance(o.get("order_ok"), bool) else None,
        registered_at=o.get("registered_at"),
        registered_at_trusted=(
            o.get("registered_at_trusted")
            if isinstance(o.get("registered_at_trusted"), bool)
            else None
        ),
        data_fetched_at=o.get("data_fetched_at"),
        data_file_mtime=o.get("data_file_mtime"),
        lock_recorded=o.get("lock_recorded") if isinstance(o.get("lock_recorded"), bool) else None,
        order_reason=o.get("order_reason"),
    )


def _read_capacity(verdict_obj: dict[str, Any]) -> Optional[Capacity]:
    diag = verdict_obj.get("diagnostics")
    if not isinstance(diag, dict):
        return None
    cap = diag.get("capacity")
    if not isinstance(cap, dict):
        return None
    return Capacity(
        K=_as_int(cap.get("K")),
        k_min=_as_int(cap.get("k_min")),
        k_min_shortfall=_as_int(cap.get("k_min_shortfall")),
        k_required_p80=_as_int(cap.get("k_required_p80")),
        k_required_p90=_as_int(cap.get("k_required_p90")),
        additional_lots_p80=_as_int(cap.get("additional_lots_p80")),
        additional_lots_p90=_as_int(cap.get("additional_lots_p90")),
        computable=cap.get("computable") if isinstance(cap.get("computable"), bool) else None,
    )


def parse_judge_result(raw: dict[str, Any]) -> JudgeResult:
    """Build a typed view from an already-parsed judge_result.json dict.

    The verdict details live under the top-level ``verdict`` object (itself holding a
    ``verdict`` string). We tolerate a flat shape too, so a caller is not broken by a
    minor layout difference, but we never invent values that are absent.
    """
    if not isinstance(raw, dict):
        raise JudgeReadError("judge_result must be a JSON object")
    vobj = raw.get("verdict")
    if not isinstance(vobj, dict):
        # Flat fallback: treat the top level as the verdict object itself.
        vobj = raw
    evidence = vobj.get("evidence") if isinstance(vobj.get("evidence"), dict) else {}
    reasons_raw = vobj.get("reasons")
    reasons = tuple(str(r) for r in reasons_raw) if isinstance(reasons_raw, list) else ()
    verdict_str = vobj.get("verdict")
    return JudgeResult(
        id=vobj.get("id") or raw.get("claim_id") or raw.get("id"),
        verdict=verdict_str if isinstance(verdict_str, str) else None,
        verdict_ja=vobj.get("verdict_ja"),
        reasons=reasons,
        rules_version=vobj.get("rules_version"),
        K=_as_int(evidence.get("K")),
        df=_as_int(evidence.get("df")),
        loo_n=_as_int(evidence.get("loo_n")),
        estimate_std=_as_float(evidence.get("estimate_std")),
        ci_std=_as_pair(evidence.get("ci_std")),
        delta_min_std=_as_float(evidence.get("delta_min_std")),
        order=_read_order(raw),
        capacity=_read_capacity(vobj),
        raw=raw,
    )


def read_judge_result(path: str | Path) -> JudgeResult:
    """Read and parse a judge_result.json file. Read-only; opens nothing else."""
    p = Path(path)
    if not p.exists():
        raise JudgeReadError("judge_result.json not found at %s" % p)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JudgeReadError("judge_result.json at %s is not valid JSON: %s" % (p, exc)) from exc
    return parse_judge_result(raw)


def read_result_folder(folder: str | Path) -> JudgeResult:
    """Read the ``judge_result.json`` inside a result folder (as produced by the
    engine, e.g. the w67 folder). The folder may also hold manifest.json and the
    engine_claim_*.json; those are not required here and are never modified.
    """
    d = Path(folder)
    jr = d / "judge_result.json"
    if not jr.exists():
        raise JudgeReadError("no judge_result.json in result folder %s" % d)
    return read_judge_result(jr)


def additional_lots_needed(result: JudgeResult, level: str = "p80") -> Optional[int]:
    """How many MORE lots the registered result says are needed at the given power
    level ('p80' or 'p90'). Read straight from diagnostics.capacity; never computed
    here. None when capacity is absent or not computable.

    This is what a class-C ('not enough lots') ledger entry records as its gap.
    """
    cap = result.capacity
    if cap is None:
        return None
    if level == "p90":
        return cap.additional_lots_p90
    return cap.additional_lots_p80
