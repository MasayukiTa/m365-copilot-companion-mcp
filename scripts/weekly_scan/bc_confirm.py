"""Processing (a): B/C confirmation driver for the weekly scan.

For every ledger row of class B or C:
  1. Count the lots now in the DB for the claim (injected LotCounter).
  2. Compare against the REGISTERED required lot count (judge's own capacity
     target, read from the registered judge_result; never recomputed).
  3. If reached, acquire a fresh confirmation dataset (injected DataFetcher)
     and run the EXISTING judge on it (injected JudgeFn), passing the
     REGISTERED lock so the registered trusted clock is used. We do not forge
     ordering: if the registered claim has no lock, order stays not-established
     and the judge caps at HOLD on its own.
  4. Update the row's last_scanned and next_trigger, and append a scan note.

Class A is never handled here (out of scope by design). B and C are always
re-examined so a non-GO analysis is never dropped from scanning.

The judge, thresholds, Delta_min and corrections are untouched: this module
only orchestrates data in / verdict out.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional, Sequence

from . import common


@dataclasses.dataclass
class BCResult:
    analysis_id: str
    klass: str
    k_now: int
    k_required: int
    reached: bool
    judged: bool
    verdict: Optional[str]
    note: str


def _registered_required(docs_root: Path, dir_rel: str) -> Optional[int]:
    """Read k_required_p80 from the registered judge_result.json under dir_rel."""
    jr = docs_root / dir_rel / "judge_result.json"
    result = common.read_registered_result(jr)
    if result is None:
        return None
    return common.registered_k_required(result, level="p80")


def _registered_lock(docs_root: Path, dir_rel: str) -> Optional[str]:
    jr = docs_root / dir_rel / "judge_result.json"
    result = common.read_registered_result(jr)
    if result is None:
        return None
    return common.registered_lock_sha256(result)


def process_bc_row(
    row: dict[str, Any],
    *,
    docs_root: Path,
    lot_counter: common.LotCounter,
    data_fetcher: common.DataFetcher,
    judge_fn: common.JudgeFn,
    today: str,
) -> BCResult:
    """Run the B/C confirmation step for a single ledger row and update it."""
    aid = str(row.get("analysis_id"))
    klass = str(row.get("class"))
    dir_rel = str(row.get("dir", "")).replace("\\", "/")

    # Required lot count: prefer the ledger's own add80/kshort-derived target,
    # else the registered judge_result capacity target. Both come from the
    # judge; we never invent a requirement.
    k_required = _registered_required(docs_root, dir_rel)
    count = lot_counter(aid, dir_rel=dir_rel)
    k_now = count.k_now
    if k_required is None:
        k_required = count.k_required

    reached = k_required is not None and k_now >= k_required

    if not reached:
        shortfall = (k_required - k_now) if k_required is not None else None
        if shortfall is not None:
            note = (
                f"{klass}: lots {k_now}/{k_required}; "
                f"{shortfall} more needed before confirmation."
            )
            nxt = (
                f"確認用ロットが規定数に未達(現在 {k_now}/{k_required}、"
                f"残り {shortfall} ロット)。次回も DB で計数。"
            )
        else:
            note = f"{klass}: required lot count unknown; kept scannable."
            nxt = "規定ロット数未定(登録結果に容量が無い)。次回も走査。"
        common.update_row(row, last_scanned=today, next_trigger=nxt, scan_note=note)
        return BCResult(aid, klass, k_now, k_required or 0, False, False,
                        row.get("verdict"), note)

    # Reached: acquire fresh confirmation data and re-judge with the REGISTERED lock.
    input_path = data_fetcher(aid, dir_rel=dir_rel)
    if not input_path:
        note = (
            f"{klass}: lots {k_now}/{k_required} reached but fresh fetch returned "
            f"no rows; recorded as acquisition issue, stays scannable."
        )
        nxt = "規定数到達だが取得データが空。再取得が必要(次回再試行)。"
        common.update_row(row, last_scanned=today, next_trigger=nxt, scan_note=note)
        return BCResult(aid, klass, k_now, k_required, True, False,
                        row.get("verdict"), note)

    lock = _registered_lock(docs_root, dir_rel)
    outcome = judge_fn(
        aid,
        input_path=input_path,
        registered_lock_sha256=lock,
        dir_rel=dir_rel,
    )
    lock_state = "registered-lock" if lock else "no-registered-lock(order stays HOLD-capped)"
    note = (
        f"{klass}: lots {k_now}/{k_required} reached; re-judged with "
        f"{lock_state} -> verdict={outcome.verdict}, reasons={','.join(outcome.reasons)}."
    )
    if outcome.verdict == "GO":
        nxt = "確認で GO(順序成立の効果あり)。採用候補として報告・追跡へ。"
    else:
        nxt = (
            f"確認で {outcome.verdict}(理由 {','.join(outcome.reasons)})。"
            "引き続き走査対象。"
        )
    common.update_row(row, last_scanned=today, next_trigger=nxt, scan_note=note)
    return BCResult(aid, klass, k_now, k_required, True, True, outcome.verdict, note)


def run_bc(
    rows: Sequence[dict[str, Any]],
    *,
    docs_root: Path,
    lot_counter: common.LotCounter,
    data_fetcher: common.DataFetcher,
    judge_fn: common.JudgeFn,
    today: Optional[str] = None,
) -> list[BCResult]:
    """Process every B and C row in the ledger (rows are mutated in place)."""
    today = today or common.today_iso()
    results: list[BCResult] = []
    for row in common.find_rows(rows, ("B", "C")):
        results.append(
            process_bc_row(
                row,
                docs_root=docs_root,
                lot_counter=lot_counter,
                data_fetcher=data_fetcher,
                judge_fn=judge_fn,
                today=today,
            )
        )
    return results
