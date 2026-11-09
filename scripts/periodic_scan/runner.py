# -*- coding: utf-8 -*-
"""Thin orchestration that wires the common base into one scan pass.

This module calls the base modules (ledger_io / judge_wrapper / db_lots) to do
only the WIRING: read the ledger -> take the re-scan classes (B/C/D) -> for C,
count lots and see whether the required number is reached -> run the judge
wrapper -> update and save the ledger.

Out of scope here (other modules own these): the concrete screening design
(confound detection, stratified estimation) and class-D re-acquisition / report
generation.

Injection:
  - DB access is an injected ``query_callable`` (``odbc_query``-like); not imported.
  - Judging is an injected ``judge_callable`` (``validity_judge``-like); not imported.
This keeps the secret judge engine out of the dependency graph and lets tests
pass fakes. No business name is hardcoded; the ODBC connection NAME comes from
the per-entry spec or the local profile.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Put scripts/ on the import path so periodic_scan imports as a package (CLI use).
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from periodic_scan import CLASS_C, RESCAN_CLASSES  # noqa: E402
from periodic_scan.db_lots import (  # noqa: E402
    LotCountResult,
    count_lots_by_product,
    default_connection,
)
from periodic_scan.judge_wrapper import JudgeVerdict, run_judge  # noqa: E402
from periodic_scan.ledger_io import (  # noqa: E402
    Ledger,
    LedgerEntry,
    load_ledger,
    save_ledger,
)


@dataclass
class ScanOutcome:
    """Result of scanning one entry once (observed facts only; no verdict edits)."""

    analysis_id: str
    classification: str
    lots_checked: bool = False
    current_lots: int | None = None
    required_lots: int | None = None
    lots_met: bool | None = None
    judged: bool = False
    verdict: str | None = None
    note: str = ""


@dataclass
class ScanReport:
    """Whole-pass result (the observations the wiring touched)."""

    scanned_at: str
    outcomes: list[ScanOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at,
            "outcomes": [vars(o) for o in self.outcomes],
        }


def _count_for_entry(
    entry: LedgerEntry,
    query_callable: Callable[..., Any],
) -> LotCountResult | None:
    """Count lots for a class-C entry, stratified by product.

    The column info needed to count lives in entry.notes['lot_count'] (written by
    the scan body). Without it, nothing is counted (returns None). The SQL is
    built by db_lots with bind parameters; the connection NAME comes from the
    spec or the local profile, never a hardcoded system name.
    """
    spec = entry.notes.get("lot_count") if isinstance(entry.notes, dict) else None
    if not isinstance(spec, dict):
        return None
    try:
        table = spec["table"]
        unit_column = spec["unit_column"]
        product_column = spec["product_column"]
    except KeyError:
        return None
    return count_lots_by_product(
        query_callable,
        table=table,
        unit_column=unit_column,
        product_column=product_column,
        products=spec.get("products"),
        connection=spec.get("connection") or default_connection(),
        where_sql=spec.get("where_sql"),
        where_params=spec.get("where_params"),
    )


def scan_entry(
    entry: LedgerEntry,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
) -> ScanOutcome:
    """Scan one entry (wiring only).

    - class C: if query_callable is present, count lots and record whether
      required_lots is reached. If reached (and config_id + judge_callable are
      present), run the registered claim through the judge.
    - class B/D: if config_id + judge_callable are present, run the judge (the
      registration-order clock is reused from the registered request; never
      forged).
    The verdict is copied only; criteria, the floor, correction and order are
    never changed here.
    """
    out = ScanOutcome(
        analysis_id=entry.analysis_id,
        classification=entry.classification,
        required_lots=entry.required_lots,
    )

    if entry.classification == CLASS_C and query_callable is not None:
        result = _count_for_entry(entry, query_callable)
        if result is not None:
            out.lots_checked = True
            have = result.total_lots
            out.current_lots = have
            entry.current_lots = have
            if entry.required_lots is not None:
                out.lots_met = have >= entry.required_lots

    should_judge = (
        judge_callable is not None
        and entry.config_id
        and entry.classification in RESCAN_CLASSES
        and (out.lots_met is not False)  # C still short: do not judge
    )
    if should_judge:
        verdict: JudgeVerdict = run_judge(entry.config_id, judge_callable)
        out.judged = True
        out.verdict = verdict.verdict
        out.note = "order_ok=%s finding=%s" % (verdict.order_ok, verdict.finding)

    if scanned_at:
        entry.last_scanned = scanned_at
    return out


def scan_ledger(
    ledger: Ledger,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
) -> ScanReport:
    """Scan the re-scan classes (B/C/D) in order and collect observations.

    The ledger (entry.current_lots / last_scanned) is updated in place here;
    saving is the caller's job (run_once saves at the end). A/E are out of scope.
    """
    report = ScanReport(scanned_at=scanned_at)
    for entry in ledger.by_classification(*sorted(RESCAN_CLASSES)):
        report.outcomes.append(
            scan_entry(
                entry,
                query_callable=query_callable,
                judge_callable=judge_callable,
                scanned_at=scanned_at,
            )
        )
    return report


def run_once(
    ledger_path: str,
    query_callable: Callable[..., Any] | None = None,
    judge_callable: Callable[..., Any] | None = None,
    scanned_at: str = "",
    save: bool = True,
) -> ScanReport:
    """Read the ledger, scan the re-scan classes once, save, and return the report.

    With no injection (dry-run) it counts no lots and judges nothing: it only
    enumerates the targets and updates last_scanned, so the wiring can be checked
    without real data.
    """
    ledger = load_ledger(ledger_path)
    report = scan_ledger(
        ledger,
        query_callable=query_callable,
        judge_callable=judge_callable,
        scanned_at=scanned_at,
    )
    if save:
        save_ledger(ledger_path, ledger)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry (dry-run). Real DB/judge need injection, so this enumerates the
    re-scan targets, updates last_scanned and saves.

    Example:
        python -m periodic_scan.runner --ledger <ledger.json> --scanned-at 2026-11-04
    """
    ap = argparse.ArgumentParser(description="periodic scan common-base dry-run")
    ap.add_argument("--ledger", required=True, help="path to the ledger JSON")
    ap.add_argument("--scanned-at", default="", help="scan date (ISO 8601)")
    ap.add_argument("--no-save", action="store_true", help="do not save the ledger")
    a = ap.parse_args(argv)
    report = run_once(
        a.ledger,
        scanned_at=a.scanned_at,
        save=not a.no_save,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "ScanOutcome",
    "ScanReport",
    "scan_entry",
    "scan_ledger",
    "run_once",
]


if __name__ == "__main__":
    sys.exit(main())
