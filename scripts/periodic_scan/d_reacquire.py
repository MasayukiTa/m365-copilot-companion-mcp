"""Processing (b): D re-acquisition.

Class D rows are analyses whose data were mis-acquired or mis-entered (period
over-restricted, a join failed, the product was not aligned in the comparison,
no rows, pure loading/confounding suspected). They cannot be confirmed as-is;
the fix is to RE-ACQUIRE the data correctly and run the frozen judge again on
the repaired input.

Each D row is processed by:
  1. loading its registered judge_request (request_loader),
  2. re-acquiring a corrected dataset (injected DataFetcher.fetch),
  3. running the FROZEN judge on the repaired input, carrying the recorded
     registration trust clock forward verbatim (never minting a new one),
  4. updating the row's last_scanned and next_trigger from the outcome.

The judge, criteria, the effect-size floor and the correction are unchanged; the
registration order clock is never forged. Re-acquisition only repairs the INPUT;
whether that now satisfies order/lots/floor is the judge's call on the real clock.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .common import (
    DataFetcher,
    JudgeRunner,
    ScanOutcome,
    ledger_key,
    reasons_of,
    registered_at_of,
    today_utc,
    verdict_of,
    with_registered_clock,
)

RequestLoader = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]

TARGET_CLASS = "D"


def reacquire_one(
    row: Mapping[str, Any],
    *,
    request_loader: RequestLoader,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
    scanned_on: Optional[str] = None,
) -> ScanOutcome:
    """Re-acquire and re-judge a single D row."""
    scanned_on = scanned_on or today_utc()
    key = ledger_key(row)
    cls = str(row.get("class", ""))
    if cls != TARGET_CLASS:
        return ScanOutcome(
            key=key, action="d_skipped_wrong_class", scanned_on=scanned_on,
            next_trigger=str(row.get("next_trigger", "")),
            detail="class=%s not D" % cls,
        )

    request = request_loader(row)
    if request is None:
        return ScanOutcome(
            key=key, action="d_no_request", scanned_on=scanned_on,
            next_trigger="no registered judge_request found; register before re-acquiring.",
            detail="request_loader returned None",
        )

    data_location = data_fetcher.fetch(request)

    # Re-acquisition repairs the INPUT only. If the original had a recorded
    # registration clock, carry it forward verbatim; if it did not, re-judge the
    # repaired input WITHOUT inventing one (re-acquisition does not create an
    # order it never had).
    if registered_at_of(request) is not None:
        req = with_registered_clock(request, data_location)
    else:
        req = dict(request)
        req["data_location"] = data_location

    result = judge.run(req)
    verdict = verdict_of(result)
    reasons = reasons_of(result)

    if verdict == "GO":
        trigger = "adopted (ordered effect present after re-acquisition); track periodically only."
        action = "d_redone_go"
    elif verdict in ("ERROR", None):
        trigger = (
            "re-acquired but input defects remain (verdict=%s); revise the "
            "acquisition conditions and redo." % (verdict or "None")
        )
        action = "d_redone_error"
    else:
        trigger = (
            "input corrected by re-acquisition but verdict=%s; re-evaluate the "
            "class and track on the matching trigger (B/C confirmation etc.)." % verdict
        )
        action = "d_redone_rejudged"
    return ScanOutcome(
        key=key, action=action, scanned_on=scanned_on, next_trigger=trigger,
        verdict=verdict, reasons=reasons,
        detail="reacquired=%s" % data_location,
    )


def reacquire_rows(
    rows,
    *,
    request_loader: RequestLoader,
    data_fetcher: DataFetcher,
    judge: JudgeRunner,
    scanned_on: Optional[str] = None,
) -> list[ScanOutcome]:
    """Run reacquire_one over every D row; non-D rows are skipped silently."""
    out: list[ScanOutcome] = []
    for row in rows:
        if str(row.get("class", "")) != TARGET_CLASS:
            continue
        out.append(
            reacquire_one(
                row, request_loader=request_loader, data_fetcher=data_fetcher,
                judge=judge, scanned_on=scanned_on,
            )
        )
    return out


__all__ = ["TARGET_CLASS", "reacquire_one", "reacquire_rows"]
