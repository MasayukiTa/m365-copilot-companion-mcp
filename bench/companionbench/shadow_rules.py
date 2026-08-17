"""Score the OLD and NEW delivery rules over the same recorded observations.

WHY THIS EXISTS. The detector was changed, the numbers moved, and the explanation offered for
the move was "the old check had a hydration race: it stopped at the first `ok` response
whatever that response contained, so a view that had not finished rendering read as 'the
marker is not here'". That is a mechanism I find plausible, which is exactly the reason not to
believe it. It explains away an inconvenient earlier result, it was proposed by the person
whose result it rescues, and the old detector cannot be re-run against the old conditions --
that day's tenant, browser and page state are gone.

What CAN be done is to stop the two rules being measured on different data. Each `/history`
attempt now records `(ok, found, truncated, users, at_s)`, so both rules can be scored on the
same rows afterwards:

    old rule   the verdict at the FIRST attempt that came back `ok`
    new rule   the verdict the loop actually returned

The interesting cell is `rescued`: turns the old rule would have called absent and the new one
found, with no second send in between -- there is no path in this loop that re-sends, so a
marker appearing on a later look can only have been rendered late. A high count is direct,
current evidence for the hydration mechanism. A count of zero says the mechanism is not
operating here, whatever happened on the earlier day, and the earlier result then needs some
other explanation.

WHAT THIS STILL CANNOT DO. It is evidence about the rules under TODAY's conditions. Projecting
it backwards needs the target fingerprinted, which the bridge does not report -- the saved
runs say `harness UNKNOWN` in as many words. So this narrows the question; it does not close
it, and `verdict()` says so rather than leaving a reader to assume otherwise.
"""
from __future__ import annotations


def old_verdict(attempt_log) -> object:
    """What the pre-retry rule would have said: the first `ok` response decides.

    Returns True (marker found), False (ok, but no marker), or None (never got an `ok`).
    """
    for attempt in attempt_log or []:
        if attempt.get("ok"):
            return bool(attempt.get("found"))
    return None


def new_verdict(attempt_log) -> object:
    """What the retrying rule saw: found on any attempt, else the last `ok` decides."""
    log = attempt_log or []
    if any(a.get("found") for a in log):
        return True
    return False if any(a.get("ok") for a in log) else None


def compare(rows) -> dict:
    """Score both rules over rows that carry an `attempt_log`.

    `rows` may be result rows (`delivery_attempt_log`) or transcript entries
    (`attempt_log`) -- both are accepted so this can be pointed at a saved run without
    reshaping it first.
    """
    counts = {"rescued": 0, "agreed_found": 0, "agreed_absent": 0,
              "agreed_unknown": 0, "reversed": 0, "other": 0}
    latencies, rescued_rows, scored = [], [], 0
    for row in rows or []:
        log = row.get("attempt_log") or row.get("delivery_attempt_log")
        if not log:
            continue
        scored += 1
        old, new = old_verdict(log), new_verdict(log)
        if old is False and new is True:
            counts["rescued"] += 1
            rescued_rows.append(row.get("episode_id") or row.get("nonce") or "?")
            latencies.append(log[-1].get("at_s") or 0)
        elif old is True and new is True:
            counts["agreed_found"] += 1
        elif old is False and new is False:
            counts["agreed_absent"] += 1
        elif old is None and new is None:
            counts["agreed_unknown"] += 1
        elif old is True and new is not True:
            # The new rule cannot lose a marker the old one saw -- if this is ever non-zero
            # the replay itself is wrong, so it is counted rather than assumed impossible.
            counts["reversed"] += 1
        else:
            counts["other"] += 1
    out = {"scored": scored, **counts,
           "rescue_rate": round(counts["rescued"] / scored, 4) if scored else None,
           "rescued_ids": rescued_rows[:20]}
    if latencies:
        latencies.sort()
        out["rescue_latency_s"] = {"min": latencies[0], "max": latencies[-1],
                                   "median": latencies[len(latencies) // 2]}
    out["verdict"] = verdict(out)
    return out


def verdict(summary) -> str:
    """What the counts license as a statement -- and what they do not."""
    if not summary.get("scored"):
        return ("no rows carried an attempt log, so neither rule was scored; this says "
                "nothing about either")
    if summary.get("reversed"):
        return ("the new rule LOST %d marker(s) the old rule saw, which cannot happen if the "
                "replay is correct -- fix the replay before reading anything else here"
                % summary["reversed"])
    if summary["rescued"]:
        return ("%d of %d turns were absent on the first `ok` look and present on a later "
                "one, with no re-send in between: the marker was rendered late. That is "
                "current evidence for the hydration mechanism, on today's conditions. It does "
                "not establish that the earlier run's negatives had the same cause -- the "
                "target is not fingerprinted, so the two eras cannot be tied together."
                % (summary["rescued"], summary["scored"]))
    return ("no turn was rescued by retrying in %d scored turns, so the hydration mechanism "
            "is not operating here. The earlier negatives therefore need an explanation this "
            "run does not supply -- 'the old detector had a race' is unsupported by this "
            "sample rather than confirmed by it." % summary["scored"])
