# -*- coding: utf-8 -*-
"""Runner for processing (a) B/C confirmation and (b) D re-acquisition.

This is the NON-TEST entry point that wires the two processors to the backlog
ledger and the injected adapters. The job loads the ledger, runs B/C
confirmation and D re-acquisition over it, writes each row's outcome back
(last_scanned / next_trigger / verdict) and reports what it did.

The judge, its criteria, the effect-size floor and the correction are NEVER
changed here, and the registration-order trust clock is NEVER forged -- both
guarantees live in the processors and in ``common``; this runner only sequences
them and persists the ledger.

The adapters (LotCounter / DataFetcher / JudgeRunner) and the request_loader are
INJECTED. ``build_run`` lets a caller assemble a run from ready-made ports
without this module importing anything heavy.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

from . import bc_confirm, d_reacquire
from .common import (
    DataFetcher,
    JudgeRunner,
    LotCounter,
    ScanOutcome,
    apply_outcome,
    ledger_key,
    today_utc,
)

RequestLoader = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]


def load_ledger(path: str) -> list[dict[str, Any]]:
    """Read the ledger JSON into a list of rows (a JSON array of objects)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, Mapping) and isinstance(data.get("rows"), list):
        return [dict(r) for r in data["rows"]]
    if isinstance(data, list):
        return [dict(r) for r in data]
    raise ValueError("ledger must be a JSON array of rows or {rows:[...]}")


def save_ledger(path: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write rows back to the ledger JSON, preserving the top-level array shape."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(list(rows), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def run_over_ledger(
    rows: Sequence[Mapping[str, Any]],
    *,
    request_loader: RequestLoader,
    lot_counter: LotCounter,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
    scanned_on: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[ScanOutcome]]:
    """Run B/C confirmation and D re-acquisition over the ledger.

    Returns the UPDATED rows (every input row, in order, with B/C and D rows
    carrying their new last_scanned / next_trigger / verdict) and the list of
    outcomes for the run log. Rows of other classes (A/E/GO) pass through
    unchanged -- this runner owns only (a) and (b).
    """
    scanned_on = scanned_on or today_utc()

    bc_outcomes = bc_confirm.confirm_rows(
        rows, request_loader=request_loader, lot_counter=lot_counter,
        data_fetcher=data_fetcher, judge=judge, scanned_on=scanned_on,
    )
    d_outcomes = d_reacquire.reacquire_rows(
        rows, request_loader=request_loader, data_fetcher=data_fetcher,
        judge=judge, scanned_on=scanned_on,
    )
    outcomes = list(bc_outcomes) + list(d_outcomes)

    by_key: dict[tuple[str, str], ScanOutcome] = {}
    for oc in outcomes:
        by_key[oc.key] = oc

    updated: list[dict[str, Any]] = []
    for row in rows:
        oc = by_key.get(ledger_key(row))
        updated.append(apply_outcome(row, oc) if oc is not None else dict(row))
    return updated, outcomes


def format_run_log(outcomes: Sequence[ScanOutcome], scanned_on: str) -> str:
    """A plain-text summary of one run, for the run record."""
    lines = ["# periodic-scan B/C confirm + D re-acquire run log (%s)" % scanned_on, ""]
    if not outcomes:
        lines.append("no B/C/D rows in scope.")
        return "\n".join(lines) + "\n"
    for oc in outcomes:
        aid, d = oc.key
        v = oc.verdict or "-"
        lines.append(
            "- %s [%s] (%s): verdict=%s / next=%s"
            % (aid, d, oc.action, v, oc.next_trigger)
        )
    return "\n".join(lines) + "\n"


def build_run(
    *,
    request_loader: RequestLoader,
    lot_counter: LotCounter,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
) -> Callable[[Sequence[Mapping[str, Any]], Optional[str]],
              tuple[list[dict[str, Any]], list[ScanOutcome]]]:
    """Bind the injected ports into a single callable the job invokes."""
    def _run(rows, scanned_on=None):
        return run_over_ledger(
            rows, request_loader=request_loader, lot_counter=lot_counter,
            data_fetcher=data_fetcher, judge=judge, scanned_on=scanned_on,
        )
    return _run


def _load_ports(wiring_module: str):
    """Import the wiring module and return its four ports."""
    import importlib

    mod = importlib.import_module(wiring_module)
    return (
        mod.request_loader,
        mod.lot_counter(),
        mod.data_fetcher(),
        mod.judge(),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger", help="path to the backlog ledger JSON")
    ap.add_argument(
        "--wiring", required=True,
        help="import path of the module providing request_loader/lot_counter/"
             "data_fetcher/judge (the real ODBC + frozen-judge wiring)",
    )
    ap.add_argument("--run-log", default=None, help="write a run log to this path")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="do not write the ledger back; print the run log only",
    )
    a = ap.parse_args(argv)

    rows = load_ledger(a.ledger)
    request_loader, lot_counter, data_fetcher, judge = _load_ports(a.wiring)
    run = build_run(
        request_loader=request_loader, lot_counter=lot_counter,
        data_fetcher=data_fetcher, judge=judge,
    )
    scanned_on = today_utc()
    updated, outcomes = run(rows, scanned_on)

    log = format_run_log(outcomes, scanned_on)
    if a.run_log:
        with open(a.run_log, "w", encoding="utf-8") as fh:
            fh.write(log)
    if not a.dry_run:
        save_ledger(a.ledger, updated)
    sys.stdout.write(log)
    return 0


__all__ = [
    "load_ledger",
    "save_ledger",
    "run_over_ledger",
    "format_run_log",
    "build_run",
]


if __name__ == "__main__":
    sys.exit(main())
