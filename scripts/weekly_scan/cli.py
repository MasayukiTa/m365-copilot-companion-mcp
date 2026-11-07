"""CLI entry for the B/C-confirmation and D-re-acquisition weekly-scan drivers.

This is the (a)+(b) slice of the weekly scan (subtask 3/5). Screening (c) and
reporting (d) live in sibling slices and are not invoked here.

The concrete DB lot counter, data fetcher and judge runner are provided by the
shared foundation (subtask 2). Until that is wired, this CLI supports --dry-run,
which uses no-op providers so the orchestration, ledger round-trip and summary
can be exercised on real ledger data without touching the DB or the judge.

Usage (one command):
    python -m scripts.weekly_scan.cli --ledger <path-to-台帳.json> \
        --docs-root <C:/Users/.../Documents> [--dry-run]

Exit code is 0 on success. The ledger is only written back when at least one
row was processed and --no-write was not given.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import bc_confirm, common, d_reacquire


class _DryLotCounter:
    """Dry-run lot counter: reports current = registered required so nothing is
    judged (reached is a no-op here because fetch is also dry). It reads the
    registered k_required so the summary is honest about the target."""

    def __init__(self, docs_root: Path) -> None:
        self._docs_root = docs_root

    def __call__(self, analysis_id: str, *, dir_rel: str) -> common.LotCount:
        jr = self._docs_root / dir_rel / "judge_result.json"
        result = common.read_registered_result(jr)
        k_req = None
        if result is not None:
            k_req = common.registered_k_required(result, level="p80")
        # Report k_now below requirement so dry-run never triggers a (no-op) judge.
        k_req_i = int(k_req) if isinstance(k_req, int) else 0
        return common.LotCount(analysis_id=analysis_id, k_now=0, k_required=k_req_i)


def _dry_fetch(analysis_id: str, *, dir_rel: str) -> Optional[str]:
    return None


def _dry_judge(
    analysis_id: str,
    *,
    input_path: str,
    registered_lock_sha256: Optional[str],
    dir_rel: str,
) -> common.JudgeOutcome:
    # Never reached in dry-run (fetch returns None), but defined to satisfy the
    # contract and to fail loudly if ever called.
    raise RuntimeError("dry-run judge must not be invoked")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="weekly_scan.cli", description=__doc__)
    p.add_argument("--ledger", required=True, help="path to 台帳.json")
    p.add_argument(
        "--docs-root",
        required=True,
        help="root the ledger 'dir' fields are relative to (the Documents folder)",
    )
    p.add_argument("--dry-run", action="store_true", help="use no-op providers")
    p.add_argument("--no-write", action="store_true", help="do not write the ledger back")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ledger_path = Path(args.ledger)
    docs_root = Path(args.docs_root)

    rows = common.load_ledger(ledger_path)
    today = common.today_iso()

    if args.dry_run:
        lot_counter: common.LotCounter = _DryLotCounter(docs_root)
        data_fetcher: common.DataFetcher = _dry_fetch
        judge_fn: common.JudgeFn = _dry_judge
    else:
        # Concrete providers come from the shared foundation (subtask 2). Import
        # lazily so --dry-run works even before that module exists.
        try:
            from scripts.weekly_scan_foundation import (  # type: ignore
                make_data_fetcher,
                make_judge_fn,
                make_lot_counter,
            )
        except Exception as exc:  # pragma: no cover - exercised once subtask 2 lands
            print(
                "error: shared foundation (subtask 2) not available; "
                f"run with --dry-run or wire it in. ({exc})",
                file=sys.stderr,
            )
            return 2
        lot_counter = make_lot_counter(docs_root)
        data_fetcher = make_data_fetcher(docs_root)
        judge_fn = make_judge_fn(docs_root)

    bc_results = bc_confirm.run_bc(
        rows,
        docs_root=docs_root,
        lot_counter=lot_counter,
        data_fetcher=data_fetcher,
        judge_fn=judge_fn,
        today=today,
    )
    d_results = d_reacquire.run_d(
        rows,
        docs_root=docs_root,
        data_fetcher=data_fetcher,
        judge_fn=judge_fn,
        today=today,
    )

    processed = len(bc_results) + len(d_results)
    if processed and not args.no_write:
        common.save_ledger(ledger_path, rows)

    bc_judged = sum(1 for r in bc_results if r.judged)
    bc_go = sum(1 for r in bc_results if r.verdict == "GO")
    d_judged = sum(1 for r in d_results if r.judged)
    d_go = sum(1 for r in d_results if r.verdict == "GO")
    print(
        f"weekly-scan (a)+(b) {today}: "
        f"B/C rows={len(bc_results)} judged={bc_judged} GO={bc_go}; "
        f"D rows={len(d_results)} re-judged={d_judged} GO={d_go}; "
        f"written={'no' if args.no_write else ('yes' if processed else 'no')}."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
