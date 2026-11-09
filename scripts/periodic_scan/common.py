# -*- coding: utf-8 -*-
"""Shared contract for the periodic-scan processors.

This module DEFINES the signatures the B/C and D processors depend on, as
:class:`typing.Protocol` adapters injected at call time. A real wiring (ODBC +
the judge harness) can satisfy these Protocols without this file importing
anything heavy; the unit tests satisfy them with in-memory fakes.

NOTHING here calls the judge, touches a DB, or mutates criteria. It is pure data
+ pure ledger helpers, and it hardcodes no business name.

Ledger row shape (as found in the backlog ledger):
    analysis_id:   str        ids CAN repeat across directories, so
                              (analysis_id, dir) is the real key.
    dir:           str
    verdict:       str | None
    reasons:       str
    K:             int | None
    ci_std:        [float, float] | None
    dmin:          float | None
    add80:         int | None   additional lots to reach the p80 capacity guard
    kshort:        int | None   lots still short of k_min
    class:         'A'|'B'|'C'|'D'|'E'|'GO'
    next_trigger:  str
    basis:         str
    last_scanned:  str  (YYYY-MM-DD)
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence


# --- the exact ledger key ---------------------------------------------------


def ledger_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """The real identity of a ledger row.

    analysis_id is NOT unique on its own (the same id can appear under two
    directories), so a scan that updated by analysis_id alone would stamp the
    wrong row. The key is (analysis_id, dir).
    """
    return (str(row.get("analysis_id", "")), str(row.get("dir", "")))


# --- requirement arithmetic (pure) -----------------------------------------


def lots_still_required(row: Mapping[str, Any]) -> Optional[int]:
    """How many more lots this row needs before a confirmation is worth running.

    For class C the binding figure is the larger of:
      * kshort  -- lots still short of k_min, and
      * add80   -- additional lots to reach the p80 capacity guarantee.
    Either may be absent (None); a present 0 means that constraint is already
    satisfied. Returns None only when BOTH are unknown -- the caller then cannot
    decide readiness from the ledger and must treat the row as not-ready.
    """
    vals = [v for v in (row.get("kshort"), row.get("add80")) if isinstance(v, int)]
    if not vals:
        return None
    return max(vals)


def is_ready_for_confirmation(row: Mapping[str, Any], new_lot_count: int) -> bool:
    """True when newly-arrived lots have reached the required number.

    ``new_lot_count`` is how many NEW (post-registration) lots the DB now holds
    for this analysis's scope. Readiness requires the row to still need a
    positive number of lots AND the new lots to have met it. A row that needs
    None (unknown) is never auto-ready -- silence is not sufficiency.
    """
    need = lots_still_required(row)
    if need is None:
        return False
    return new_lot_count >= need


# --- injected adapters (Protocols) -----------------------------------------


class LotCounter(Protocol):
    """Counts NEW lots in the DB for a B/C analysis's registered scope.

    MUST count only lots that post-date registration (the confirmation set must
    be disjoint from the data the claim was registered on). Implementations read
    the registered stratification from the request; they never relax the
    effect-size floor or k_min.
    """

    def count_new_lots(self, request: Mapping[str, Any]) -> int: ...


class DataFetcher(Protocol):
    """Fetches a fresh confirmation dataset for an analysis and returns a path.

    For B/C this is the NEW-lot confirmation set; for D this is the corrected
    re-acquisition. The returned path is what the judge hashes as input.
    """

    def fetch(self, request: Mapping[str, Any]) -> str: ...


class JudgeRunner(Protocol):
    """Runs the FROZEN validity judge on a request and returns its result dict.

    Implementations invoke the real engine unchanged; this package never
    re-implements it.
    """

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


# --- reuse of the registered trust clock (NEVER forged) --------------------


def registered_at_of(request: Mapping[str, Any]) -> Optional[str]:
    """The already-registered registration time, read verbatim.

    Looks in both places the harness writes it: registration.registered_at and
    order.registered_at. Returns None if neither is present -- the caller must
    then NOT invent one (that would be forging the order clock).
    """
    reg = request.get("registration")
    if isinstance(reg, Mapping) and reg.get("registered_at"):
        return str(reg["registered_at"])
    order = request.get("order")
    if isinstance(order, Mapping) and order.get("registered_at"):
        return str(order["registered_at"])
    if request.get("registered_at"):
        return str(request["registered_at"])
    return None


def with_registered_clock(
    request: Mapping[str, Any], confirmation_data_location: str
) -> dict[str, Any]:
    """Build a confirmation request that REUSES the registered trust clock.

    The confirmation points the judge at new data but carries the ORIGINAL
    ``registered_at`` forward unchanged. It never sets a registration time
    later/earlier to make the order pass -- that decision belongs to the judge on
    the real recorded clock.

    Raises ValueError if no registered time exists, because silently minting one
    is exactly the forgery this package forbids.
    """
    reg_at = registered_at_of(request)
    if reg_at is None:
        raise ValueError(
            "no registered_at on the original request; refusing to mint a "
            "registration time (would forge the order clock)"
        )
    out = dict(request)
    out["data_location"] = confirmation_data_location
    reg = dict(out.get("registration") or {})
    reg["registered_at"] = reg_at
    out["registration"] = reg
    out["reused_registered_at"] = reg_at
    return out


# --- verdict reading --------------------------------------------------------


def verdict_of(result: Mapping[str, Any]) -> Optional[str]:
    """Top-level verdict string from a judge result (GO/HOLD/NA/ERROR)."""
    v = result.get("verdict")
    if isinstance(v, Mapping):
        return v.get("verdict")
    if isinstance(v, str):
        return v
    return None


def reasons_of(result: Mapping[str, Any]) -> list[str]:
    v = result.get("verdict")
    if isinstance(v, Mapping) and isinstance(v.get("reasons"), Sequence):
        return [str(r) for r in v["reasons"]]
    r = result.get("reasons")
    if isinstance(r, Sequence) and not isinstance(r, str):
        return [str(x) for x in r]
    return []


# --- ledger update (pure; returns a new row) -------------------------------


def today_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ScanOutcome:
    """What one processor did to one row, for the ledger and the run log."""

    key: tuple[str, str]
    action: str                 # e.g. 'bc_not_ready', 'bc_confirmed', 'd_redone'
    scanned_on: str
    next_trigger: str
    verdict: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    detail: str = ""


def apply_outcome(row: Mapping[str, Any], outcome: ScanOutcome) -> dict[str, Any]:
    """Return a COPY of row with last_scanned / next_trigger (and verdict when
    the judge produced one) updated from the outcome. Other fields untouched.
    """
    out = dict(row)
    out["last_scanned"] = outcome.scanned_on
    out["next_trigger"] = outcome.next_trigger
    if outcome.verdict is not None:
        out["verdict"] = outcome.verdict
    if outcome.reasons:
        out["reasons"] = ",".join(outcome.reasons)
    prior = str(out.get("scan_log") or "").strip()
    stamp = "%s %s" % (outcome.scanned_on, outcome.action)
    out["scan_log"] = (prior + "; " + stamp).strip("; ") if prior else stamp
    return out


__all__ = [
    "ledger_key",
    "lots_still_required",
    "is_ready_for_confirmation",
    "LotCounter",
    "DataFetcher",
    "JudgeRunner",
    "registered_at_of",
    "with_registered_clock",
    "verdict_of",
    "reasons_of",
    "today_utc",
    "ScanOutcome",
    "apply_outcome",
]
