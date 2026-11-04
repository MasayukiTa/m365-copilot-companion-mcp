"""Processing (b): D re-acquisition.

Class D rows are analyses whose data were mis-acquired or mis-entered (period
over-restricted, join failed, 品名 not aligned in the comparison, no rows, pure
loading/confounding suspected).  They cannot be confirmed as-is; the fix is to
RE-ACQUIRE the data correctly and run the frozen judge again on the repaired
input.

Each D row is processed by:
  1. loading its registered judge_request (request_loader),
  2. re-acquiring a corrected dataset (injected DataFetcher.fetch),
  3. running the FROZEN judge on the repaired input, carrying the recorded
     registration trust clock forward verbatim (never minting a new one),
  4. updating the row's last_scanned and next_trigger from the outcome.

The judge, criteria, Δmin and correction are unchanged; the registration order
clock is never forged.  Re-acquisition only repairs the INPUT; whether that now
satisfies order/lots/Δmin is the judge's call on the real clock.
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
            next_trigger="登録済み judge_request が見当たらない。再取得の前に登録が必要。",
            detail="request_loader returned None",
        )

    data_location = data_fetcher.fetch(request)

    # Re-acquisition repairs the INPUT only.  If the original had a recorded
    # registration clock, carry it forward verbatim; if it did not, re-judge the
    # repaired input WITHOUT inventing one (the judge will record D_FAIL_NO_LOCK
    # just as before -- re-acquisition does not create an order it never had).
    if registered_at_of(request) is not None:
        req = with_registered_clock(request, data_location)
    else:
        req = dict(request)
        req["data_location"] = data_location

    result = judge.run(req)
    verdict = verdict_of(result)
    reasons = reasons_of(result)

    if verdict == "GO":
        trigger = "採用(再取得後に順序成立の効果あり)。週次で追跡のみ。"
        action = "d_redone_go"
    elif verdict in ("ERROR", None):
        trigger = (
            "再取得したが入力不備が残る(verdict=%s)。取得条件を見直して再度やり直す。"
            % (verdict or "None")
        )
        action = "d_redone_error"
    else:
        trigger = (
            "再取得で入力は正しくなったが verdict=%s。分類を再評価し、"
            "該当する契機(B/Cの確認等)で追跡。" % verdict
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
