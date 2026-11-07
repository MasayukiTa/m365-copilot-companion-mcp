"""Processing (b): D re-acquisition driver for the weekly scan.

Class D rows are analyses suspected of an acquisition / input defect
(verdict ERROR with INPUT_NO_ROWS, a join/period/product-name mismatch, or a
pure-loading confound such as C_PURE_LOADING_SUSPECT). For each D row:
  1. Re-acquire the dataset (injected DataFetcher). This is the "redo".
  2. If rows come back, re-run the EXISTING judge on the fresh input, passing
     the REGISTERED lock so the registered trusted clock is used (never forged).
  3. If still no rows, keep it as D (still scannable) and record why.
  4. Update last_scanned / next_trigger and append a scan note.

The judge, thresholds, Delta_min and corrections are untouched.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional, Sequence

from . import common


@dataclasses.dataclass
class DResult:
    analysis_id: str
    reacquired: bool        # fetch returned a usable input path
    judged: bool            # judge was run on fresh data
    verdict: Optional[str]
    note: str


def _registered_lock(docs_root: Path, dir_rel: str) -> Optional[str]:
    jr = docs_root / dir_rel / "judge_result.json"
    result = common.read_registered_result(jr)
    if result is None:
        return None
    return common.registered_lock_sha256(result)


def process_d_row(
    row: dict[str, Any],
    *,
    docs_root: Path,
    data_fetcher: common.DataFetcher,
    judge_fn: common.JudgeFn,
    today: str,
) -> DResult:
    """Re-acquire and re-judge a single class-D ledger row; update it in place."""
    aid = str(row.get("analysis_id"))
    dir_rel = str(row.get("dir", "")).replace("\\", "/")
    reasons_before = str(row.get("reasons", ""))

    input_path = data_fetcher(aid, dir_rel=dir_rel)
    if not input_path:
        note = (
            f"D: re-acquire returned no rows (was: {reasons_before}); "
            "input/acquisition still unresolved, stays D and scannable."
        )
        nxt = "再取得したが行が空。取得・結合・期間条件を見直して次回再試行。"
        common.update_row(row, last_scanned=today, next_trigger=nxt, scan_note=note)
        return DResult(aid, False, False, row.get("verdict"), note)

    lock = _registered_lock(docs_root, dir_rel)
    outcome = judge_fn(
        aid,
        input_path=input_path,
        registered_lock_sha256=lock,
        dir_rel=dir_rel,
    )
    lock_state = "registered-lock" if lock else "no-registered-lock(order stays HOLD-capped)"
    note = (
        f"D: re-acquired and re-judged with {lock_state} "
        f"-> verdict={outcome.verdict}, reasons={','.join(outcome.reasons)} "
        f"(was: {reasons_before})."
    )
    if outcome.verdict == "GO":
        nxt = "再取得後の確認で GO。採用候補として報告・追跡へ。"
    elif outcome.verdict == "ERROR":
        nxt = (
            f"再取得後も ERROR(理由 {','.join(outcome.reasons)})。"
            "取得・入力の不備が残る。次回再試行。"
        )
    else:
        nxt = (
            f"再取得後は {outcome.verdict}(理由 {','.join(outcome.reasons)})。"
            "引き続き走査対象。"
        )
    common.update_row(row, last_scanned=today, next_trigger=nxt, scan_note=note)
    return DResult(aid, True, True, outcome.verdict, note)


def run_d(
    rows: Sequence[dict[str, Any]],
    *,
    docs_root: Path,
    data_fetcher: common.DataFetcher,
    judge_fn: common.JudgeFn,
    today: Optional[str] = None,
) -> list[DResult]:
    """Process every D row in the ledger (rows are mutated in place)."""
    today = today or common.today_iso()
    results: list[DResult] = []
    for row in common.find_rows(rows, ("D",)):
        results.append(
            process_d_row(
                row,
                docs_root=docs_root,
                data_fetcher=data_fetcher,
                judge_fn=judge_fn,
                today=today,
            )
        )
    return results
