# -*- coding: utf-8 -*-
"""Thin wrapper around the existing validity judge.

Design:
  - The judge engine lives outside this repository and is locked by
    pre-registration (byte-identical). This wrapper never edits, copies or
    re-implements the engine, its criteria, effect-size floor or correction.
  - Judging is reached through one injected callable (the ``validity_judge``
    tool, or a test fake). The registration-order trust clock is reused from the
    already-registered request; this wrapper never mints or forges one.

Provides:
  1. ``JudgeVerdict``         : a read-only normalised view of a judge result
  2. ``parse_judge_result()`` : normalise a judge result into ``JudgeVerdict``
  3. ``run_judge()``          : call the injected judge and normalise its result

``run_judge`` imports no tool; it takes ``judge_callable`` and calls
``judge_callable(config_id=..., engine_version=...)``. Surfaced fields match the
harness: id / verdict / finding / reasons / notes / hashes / timestamps only.
Raw measurement rows and personal identifiers are never read or returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

VERDICT_GO = "GO"
VERDICT_HOLD = "HOLD"
VERDICT_NA = "NA"
VERDICT_ERROR = "ERROR"


@dataclass
class JudgeVerdict:
    """The essentials of a judge result (read-only normalised view).

    The result is deeply nested, with same-named fields at the top level and in
    a nested ``verdict`` object. This view prefers the nested ``verdict`` level
    and falls back to the top level.
    """

    claim_id: str
    verdict: str
    claim_supported: bool | None = None
    finding: str | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    order_ok: bool | None = None
    estimate_std: float | None = None
    ci_std: list[float] | None = None
    delta_min: float | None = None
    K: int | None = None
    df: int | None = None
    hashes: dict[str, Any] = field(default_factory=dict)
    rules_version: str | None = None

    @property
    def is_go(self) -> bool:
        return str(self.verdict).strip().upper() == VERDICT_GO

    @property
    def is_hold(self) -> bool:
        return str(self.verdict).strip().upper() == VERDICT_HOLD

    @property
    def interval_inside_delta_min(self) -> bool | None:
        """Whether the standardised interval sits inside the floor (class-A cue).

        Does not override the judge's finding. Returns the mechanical in/out only
        when both the interval and the floor are present; otherwise None.
        """
        if self.ci_std is None or self.delta_min is None:
            return None
        if len(self.ci_std) != 2:
            return None
        lo, hi = self.ci_std
        return (lo >= -self.delta_min) and (hi <= self.delta_min)


def _first_present(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


def parse_judge_result(result: dict[str, Any] | str | Path) -> JudgeVerdict:
    """Normalise a judge result (dict / JSON string / file path) to JudgeVerdict.

    Copies the judge's values verbatim; never recomputes, corrects or reorders.
    """
    if isinstance(result, dict):
        data = result
    elif isinstance(result, Path):
        data = json.loads(result.read_text(encoding="utf-8"))
    else:
        # A JSON body (starting with { or [) is parsed as text, not a filename.
        # Passing a long JSON string to Path.exists() can raise 'File name too
        # long', so decide on the body first.
        s = str(result).strip()
        if s[:1] in "{[":
            data = json.loads(s)
        else:
            p = Path(result)
            try:
                is_file = p.exists()
            except OSError:
                is_file = False
            if is_file:
                data = json.loads(p.read_text(encoding="utf-8"))
            else:
                data = json.loads(s)

    if not isinstance(data, dict):
        raise ValueError("judge result must be an object")

    nested = data.get("verdict") if isinstance(data.get("verdict"), dict) else {}
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    evidence = nested.get("evidence") if isinstance(nested.get("evidence"), dict) else {}

    verdict_str = data.get("verdict") if isinstance(data.get("verdict"), str) else None
    if verdict_str is None:
        verdict_str = nested.get("verdict")

    claim_id = _first_present(
        data.get("claim_id"), nested.get("id"), data.get("id")
    )
    if not claim_id:
        raise ValueError("judge result has no claim id")

    reasons = nested.get("reasons") or data.get("reasons") or []
    notes = nested.get("notes") or data.get("notes") or []
    ci_std = _first_present(
        evidence.get("ci_std"),
        nested.get("interval", {}).get("ci_std")
        if isinstance(nested.get("interval"), dict) else None,
    )

    return JudgeVerdict(
        claim_id=str(claim_id),
        verdict=str(verdict_str) if verdict_str is not None else "",
        claim_supported=_first_present(nested.get("claim_supported")),
        finding=_first_present(nested.get("finding"), data.get("finding")),
        reasons=list(reasons),
        notes=list(notes),
        order_ok=order.get("order_ok"),
        estimate_std=evidence.get("estimate_std"),
        ci_std=list(ci_std) if isinstance(ci_std, (list, tuple)) else None,
        delta_min=_first_present(evidence.get("delta_min_std"), evidence.get("delta_min")),
        K=evidence.get("K"),
        df=evidence.get("df"),
        hashes=dict(nested.get("hashes") or {}),
        rules_version=nested.get("rules_version"),
    )


def run_judge(
    config_id: str,
    judge_callable: Callable[..., Any],
    engine_version: str = "",
) -> JudgeVerdict:
    """Call the existing judge and normalise its result to JudgeVerdict.

    Parameters:
      config_id      : id of the already-registered claim (never minted here).
      judge_callable : a callable like ``validity_judge``, invoked as
                       ``judge_callable(config_id=config_id, engine_version=...)``
                       returning a judge-result dict or JSON string.
      engine_version : engine-version selector (default empty = harness default).

    This wrapper never changes criteria, the effect-size floor, correction or the
    registration-order clock; it normalises what the judge returned.
    """
    if not config_id or not str(config_id).strip():
        raise ValueError("config_id must be a non-empty registered claim id")
    raw = judge_callable(config_id=config_id, engine_version=engine_version)
    if isinstance(raw, str):
        raw = json.loads(raw)
    return parse_judge_result(raw)


__all__ = [
    "VERDICT_GO",
    "VERDICT_HOLD",
    "VERDICT_NA",
    "VERDICT_ERROR",
    "JudgeVerdict",
    "parse_judge_result",
    "run_judge",
]
