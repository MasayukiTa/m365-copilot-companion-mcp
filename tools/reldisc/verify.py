"""Containment verification of a candidate pair under a per-pair time and row budget."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .profile import ColumnProfile
from .source import MAX_IN_BATCH, BudgetExceeded, Source


@dataclass
class PairBudget:
    time_s: float = 10.0          # wall-clock for the whole pair
    rows: int = 50_000            # rows transferred from the database for the whole pair
    sample_values: int = 2_000    # max distinct values per side tested
    batch: int = MAX_IN_BATCH


@dataclass
class VerifyResult:
    status: str                   # "verified" | "unverified"
    reason: str = ""
    a_in_b: Optional[float] = None
    b_in_a: Optional[float] = None
    jaccard: Optional[float] = None
    cardinality: str = ""
    confidence: float = 0.0
    evidence: dict = field(default_factory=dict)


def _distinct_values(source: Source, p: ColumnProfile, budget: PairBudget, seed: int,
                     cache: Optional[dict] = None) -> list:
    key = (p.table, p.column, budget.sample_values, seed)
    if cache is not None and key in cache:
        return cache[key]
    rows, complete = source.sample(p.table, [p.column], budget.sample_values, seed)
    seen: dict[Any, None] = {}
    for (v,) in rows:
        if v is not None and v != "":
            seen[v] = None
    if cache is not None:
        cache[key] = list(seen)
    return list(seen)


def _contained(source: Source, p: ColumnProfile, values: list, deadline: float,
               row_cap: int, rows_before: int, batch: int) -> tuple[int, int, str]:
    """Count how many of ``values`` occur in ``p``. Returns (matched, tested, stop_reason)."""
    matched = tested = 0
    for i in range(0, len(values), batch):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return matched, tested, "budget_time"
        if source.rows_fetched - rows_before >= row_cap:
            return matched, tested, "budget_rows"
        chunk = values[i:i + batch]
        try:
            hit = source.select_in(p.table, p.column, chunk, timeout_s=remaining)
        except BudgetExceeded:
            return matched, tested, "budget_time"
        matched += len(set(hit))
        tested += len(chunk)
    return matched, tested, ""


def _dup(p: ColumnProfile) -> float:
    return p.nonnull_rows / p.distinct_est if p.distinct_est > 0 else 1.0


def cardinality_label(a: ColumnProfile, b: ColumnProfile) -> str:
    a_many, b_many = _dup(a) > 1.2, _dup(b) > 1.2
    if a_many and b_many:
        return "N:M"
    return ("N" if a_many else "1") + ":" + ("N" if b_many else "1")


def _chance_level(a: ColumnProfile, b: ColumnProfile) -> float:
    """How often unrelated values would 'be contained' by luck: for two integer columns this
    is the density of the larger side's value range (dense surrogate ids always overlap)."""
    if a.family == "int" and b.family == "int":
        try:
            span = float(b.max_value) - float(b.min_value) + 1.0
            return min(1.0, b.distinct_est / span) if span > 0 else 1.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def score_confidence(a: ColumnProfile, b: ColumnProfile, a_in_b: float, b_in_a: float,
                     n_a: int, n_b: int, name_score: float) -> float:
    """0..1 confidence that the pair is a real join key.

    The better containment direction, corrected for chance overlap, scaled by how many values
    were actually tested and by how many distinct values the contained side has (a flag with
    three values is contained in anything)."""
    if a_in_b >= b_in_a:
        c, n, small, tgt = a_in_b, n_a, a, b
    else:
        c, n, small, tgt = b_in_a, n_b, b, a
    chance = _chance_level(small, tgt)
    base = (c - chance) / (1.0 - chance) if chance < 1.0 else 0.0
    # Dense id ranges overlap by construction; only a related name can lift them.
    rescued = c * min(0.8, name_score) if chance >= 0.5 else 0.0
    conf = max(base, rescued)
    conf *= n / (n + 20.0)
    if small.distinct_est < 5:
        conf *= 0.3
    return round(max(0.0, min(1.0, conf)), 4)


def verify_pair(source: Source, a: ColumnProfile, b: ColumnProfile, budget: Optional[PairBudget] = None,
                name_score: float = 0.0, seed: int = 0, cache: Optional[dict] = None) -> VerifyResult:
    """Measure A subset-of B, B subset-of A, overlap and cardinality for one candidate pair."""
    budget = budget or PairBudget()
    started = time.monotonic()
    deadline = started + budget.time_s
    rows_before = source.rows_fetched
    ev: dict = {"budget": {"time_s": budget.time_s, "rows": budget.rows,
                           "sample_values": budget.sample_values}, "name_score": name_score}

    def done(res: VerifyResult) -> VerifyResult:
        ev["rows_read"] = source.rows_fetched - rows_before
        ev["elapsed_s"] = round(time.monotonic() - started, 4)
        res.evidence = ev
        return res

    try:
        if time.monotonic() >= deadline:
            return done(VerifyResult("unverified", "budget_time"))
        va = _distinct_values(source, a, budget, seed, cache)
        vb = _distinct_values(source, b, budget, seed, cache)
        ev["n_a"], ev["n_b"] = len(va), len(vb)
        if not va or not vb:
            return done(VerifyResult("unverified", "empty_sample"))
        ab, ta, why = _contained(source, b, va, deadline, budget.rows, rows_before, budget.batch)
        if why:
            ev.update(partial={"a_in_b_matched": ab, "a_in_b_tested": ta})
            return done(VerifyResult("unverified", why))
        ba, tb, why = _contained(source, a, vb, deadline, budget.rows, rows_before, budget.batch)
        if why:
            ev.update(partial={"a_in_b": ab / ta if ta else None, "b_in_a_matched": ba, "b_in_a_tested": tb})
            return done(VerifyResult("unverified", why))
    except Exception as exc:  # a pair that cannot be read is recorded, not fatal to the run
        ev["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return done(VerifyResult("unverified", "error"))

    a_in_b, b_in_a = ab / ta, ba / tb
    # Overlap of the two distinct domains, estimated from the two directional rates.
    inter = 0.5 * (a_in_b * a.distinct_est + b_in_a * b.distinct_est)
    union = a.distinct_est + b.distinct_est - inter
    jaccard = round(inter / union, 4) if union > 0 else 0.0
    card = cardinality_label(a, b)
    conf = score_confidence(a, b, a_in_b, b_in_a, len(va), len(vb), name_score)
    ev.update(a_in_b=round(a_in_b, 4), b_in_a=round(b_in_a, 4), chance=_chance_level(a, b),
              a_exact=a.complete, b_exact=b.complete)
    return done(VerifyResult("verified", "", round(a_in_b, 4), round(b_in_a, 4), jaccard, card, conf))
