"""Processing (a): B/C confirmation.

For every ledger row in class B or C:
  1. Count NEW lots in the DB for the registered scope (injected LotCounter).
  2. If the new lots have reached the required number (kshort / add80), fetch a
     fresh confirmation dataset (injected DataFetcher) and run the registered
     claim through the FROZEN judge (injected JudgeRunner), REUSING the recorded
     registration trust clock (never minting a new one).
  3. Update the row's last_scanned and next_trigger from the outcome.

A B row (post-hoc, order not established) and a C row (lots short) differ only in
why they were held; both are confirmed the same way -- by gathering the required
new lots and letting the judge rule on the real clock. Neither the judge, its
criteria, the effect-size floor nor the correction is altered here, and the
order clock is never forged.

The request for a row is obtained via a ``request_loader`` callable: given the
ledger row it returns that analysis's judge_request mapping (or None if the
analysis has no registered request yet -- then the row is left not-ready, never
fabricated).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .common import (
    DataFetcher,
    JudgeRunner,
    LotCounter,
    ScanOutcome,
    is_ready_for_confirmation,
    ledger_key,
    lots_still_required,
    reasons_of,
    today_utc,
    verdict_of,
    with_registered_clock,
)

RequestLoader = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]

TARGET_CLASSES = frozenset({"B", "C"})


def _not_ready_trigger(row: Mapping[str, Any], new_lots: int) -> str:
    need = lots_still_required(row)
    if need is None:
        return (
            "lots short but the required number is unknown from the ledger "
            "(kshort and add80 both None); re-check the summary."
        )
    remaining = max(0, need - new_lots)
    return (
        "confirmation lots below the required number (new=%d, need=%d, "
        "%d to go); re-count next scan." % (new_lots, need, remaining)
    )


def confirm_one(
    row: Mapping[str, Any],
    *,
    request_loader: RequestLoader,
    lot_counter: LotCounter,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
    scanned_on: Optional[str] = None,
) -> ScanOutcome:
    """Confirm a single B/C row. Pure of I/O except through the injected ports."""
    scanned_on = scanned_on or today_utc()
    key = ledger_key(row)
    cls = str(row.get("class", ""))
    if cls not in TARGET_CLASSES:
        return ScanOutcome(
            key=key, action="bc_skipped_wrong_class", scanned_on=scanned_on,
            next_trigger=str(row.get("next_trigger", "")),
            detail="class=%s not in B/C" % cls,
        )

    request = request_loader(row)
    if request is None:
        return ScanOutcome(
            key=key, action="bc_no_request", scanned_on=scanned_on,
            next_trigger=(
                "no registered judge_request found; the claim must be "
                "registered again first."
            ),
            detail="request_loader returned None",
        )

    new_lots = int(lot_counter.count_new_lots(request))
    if not is_ready_for_confirmation(row, new_lots):
        return ScanOutcome(
            key=key, action="bc_not_ready", scanned_on=scanned_on,
            next_trigger=_not_ready_trigger(row, new_lots),
            detail="new_lots=%d" % new_lots,
        )

    # Ready: fetch confirmation data and run the FROZEN judge on the reused clock.
    data_location = data_fetcher.fetch(request)
    conf_request = with_registered_clock(request, data_location)
    result = judge.run(conf_request)
    verdict = verdict_of(result)
    reasons = reasons_of(result)

    if verdict == "GO":
        trigger = "adopted (ordered effect present); track periodically only."
        action = "bc_confirmed_go"
    else:
        trigger = (
            "confirmation ran but verdict=%s; re-confirm when new lots arrive."
            % (verdict or "None")
        )
        action = "bc_confirmed_hold"
    return ScanOutcome(
        key=key, action=action, scanned_on=scanned_on, next_trigger=trigger,
        verdict=verdict, reasons=reasons,
        detail="new_lots=%d data=%s" % (new_lots, data_location),
    )


def confirm_rows(
    rows,
    *,
    request_loader: RequestLoader,
    lot_counter: LotCounter,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
    scanned_on: Optional[str] = None,
) -> list[ScanOutcome]:
    """Run confirm_one over every B/C row; non-B/C rows are skipped silently."""
    out: list[ScanOutcome] = []
    for row in rows:
        if str(row.get("class", "")) not in TARGET_CLASSES:
            continue
        out.append(
            confirm_one(
                row, request_loader=request_loader, lot_counter=lot_counter,
                data_fetcher=data_fetcher, judge=judge, scanned_on=scanned_on,
            )
        )
    return out


__all__ = ["TARGET_CLASSES", "confirm_one", "confirm_rows"]
