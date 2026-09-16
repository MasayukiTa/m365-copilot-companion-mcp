# -*- coding: utf-8 -*-
"""Can a FLEET worker call an MCP tool? Answered from the ledger, not from a probe.

THE MIRROR OF THE GAP tools/tool_probe.py EXISTS TO CLOSE. That module's docstring says it
plainly: "FleetCockpit's health strip only probes :9222 (the FLEET Edge) and :8000/tunnel --
none of its dots ever verify that the BRIDGE-side agent can actually CALL an MCP tool
end-to-end." Dot 5 was added for the bridge. Nothing was ever added for the fleet, and the
fleet is the path that does the work.

WHAT THAT COST, 2026-09-16. Dot 5 was red -- truthfully, the bridge agent had lost its tool
list -- while fleet workers completed 84 tool calls in an hour and a goal finished DONE with
the refuter upholding it. A strip with one dot labelled "Tool" showed red. Anyone reading it
concluded tool access was down. It was not; one path of four was.

NO SYNTHETIC PROBE, DELIBERATELY. tool_probe learned the hard way that an invariant question
lets the model answer from memory without calling anything, so a green probe proved nothing
(see its second incident note). The fleet needs no such trick: every worker tool call and its
outcome is already written to .fleet/tool_events.jsonl by the gateway. Real calls, real
results, no question to memorise and no extra turn spent asking.

"NO EVIDENCE" IS A STATE, AND GETTING IT WRONG WOULD REPRODUCE THE BUG THIS FIXES. An idle
machine makes no tool calls. Reporting that as a failure would invent a second false red, so
absence of calls returns ok=None -- unknown -- and the caller is expected to render it grey
rather than red. Only observed FAILURES make this say no.

Stdlib only and import-safe, the same contract tool_probe.py holds, so /health can import it
without paying for anything heavy.
"""
from __future__ import annotations

import io
import json

import time
from pathlib import Path
from typing import Optional

from . import tool_ledger as _ledger

#: Written by the gateway on every tool call and outcome.
LEDGER = Path(__file__).resolve().parent.parent / ".fleet" / "tool_events.jsonl"

#: How far back a call still counts as evidence about NOW. Fifteen minutes is longer than a
#: worker's turn and shorter than a coffee break, so a live run always has evidence and an
#: hour-old run never masquerades as one.
FRESH_S = 900.0

#: The ledger is tens of megabytes (67 MB measured on 2026-09-16) and /health may be polled
#: every few seconds, so only the tail is read. 256 KB covered ~700 records in that file --
#: far more than FRESH_S can contain -- and costs one seek instead of a full scan.
TAIL_BYTES = 256 * 1024

#: How many trailing outcomes must ALL be failures before the path is called down. One
#: refusal among many successes is a transient -- measured: 91 ok and 1 fail in two hours,
#: in a run that finished DONE. Three consecutive is a pattern.
CONSECUTIVE_FAILURES_FOR_RED = 3


def _tail_records(path: Path, nbytes: int):
    """Yield parsed records from the last `nbytes` of a JSONL file.

    The first line after a blind seek is almost certainly a fragment, so it is dropped. Any
    unparseable line is skipped rather than raising: this feeds a health display, and a
    corrupt byte in a log must not take the display down with it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        with io.open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()  # discard the partial line
            for raw in fh:
                try:
                    yield json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
    except OSError:
        return


def get_summary(now: Optional[float] = None, fresh_s: float = FRESH_S) -> dict:
    """What the fleet's tool path has actually been doing lately.

    Returns, mirroring the shape tools/tool_probe.get_summary() publishes into /health:

        fleet_tool_ok      True  -- calls happened and the path is not currently failing
                           False -- the last CONSECUTIVE_FAILURES_FOR_RED outcomes ALL failed
                           None  -- no calls in the window; NOT a failure, render grey
        fleet_tool_ok_n    successful outcomes seen in the window
        fleet_tool_fail_n  failed outcomes seen in the window
        fleet_tool_last_s  seconds since the most recent call, or None
        fleet_tool_last_tool  the name of that most recent call, or None

    Never raises. A missing ledger, an unreadable one, or a machine that has simply been idle
    all read as "no evidence", because none of those is evidence of breakage.
    """
    now = time.time() if now is None else now
    cutoff = now - fresh_s
    calls = {}
    ok_n = fail_n = unavail_n = 0
    last_ts = None
    last_tool = None
    recent = []  # outcome successes in order, newest last
    for rec in _tail_records(LEDGER, TAIL_BYTES):
        try:
            ts = float(rec.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        event = rec.get("event")
        if event == "call":
            calls[rec.get("id")] = str(rec.get("tool") or "?")
            if last_ts is None or ts >= last_ts:
                last_ts, last_tool = ts, str(rec.get("tool") or "?")
        elif event == "outcome":
            # DISCOVERY CHATTER IS NOT THE TOOL PATH. call_tool.catalogue / .unknown /
            # .signature are the gateway answering questions about itself; they succeed even
            # when every real tool is unreachable, so counting them would manufacture green.
            if str(calls.get(rec.get("id"), "")).startswith("call_tool."):
                continue
            # THROUGH row_ok, NOT rec["ok"]. tool_ledger has corrected refusals on read since
            # the day it learned that a refusal returns normally, and this reader -- the one
            # that actually decides the colour of a dot -- was the only place that never
            # asked it. 1,650 of 37,016 rows filed ok=True hold an error report; every one of
            # them was making this indicator greener.
            if _ledger.row_unavailable(rec):
                # NOT EVIDENCE EITHER WAY, so it does not enter `recent`. The workstation was
                # locked or on the secure desktop: no screen could be captured and no click
                # delivered, and the tools were fine. Counted so the reason a dot has gone
                # grey can be stated instead of merely observed.
                unavail_n += 1
                continue
            good = _ledger.row_ok(rec)
            recent.append(good)
            if good:
                ok_n += 1
            else:
                fail_n += 1

    # "ANY FAILURE MEANS RED" WAS THE FIRST RULE AND IT WAS WRONG, caught by running this
    # rather than by reading it. Over a two-hour window the ledger held 91 successes and 1
    # failure, and that single refusal came from the run which finished DONE with the refuter
    # upholding it. Reporting that as FAILING would have invented a second false red -- the
    # exact defect this module exists to remove.
    #
    # The question a health dot answers is "can a worker call a tool NOW", so the evidence is
    # the LATEST outcomes, not the whole window's tally. Consecutive failures at the end mean
    # the path is down; one failure with successes after it is a transient that already
    # recovered. CONSECUTIVE_FAILURES_FOR_RED is 3 so a single refusal -- of which the
    # unlock-token branch produces a steady trickle -- cannot flip the display on its own.
    if not recent:
        state = None
    else:
        tail = recent[-CONSECUTIVE_FAILURES_FOR_RED:]
        state = not (len(tail) == CONSECUTIVE_FAILURES_FOR_RED and not any(tail))
        if len(recent) < CONSECUTIVE_FAILURES_FOR_RED:
            state = any(recent)

    return {
        "fleet_tool_ok": state,
        "fleet_tool_ok_n": ok_n,
        "fleet_tool_fail_n": fail_n,
        "fleet_tool_unavailable_n": unavail_n,
        "fleet_tool_last_s": None if last_ts is None else round(now - last_ts, 1),
        "fleet_tool_last_tool": last_tool,
    }


def describe(summary: Optional[dict] = None) -> str:
    """One line for a human, saying which of the three states this is and on what evidence."""
    s = get_summary() if summary is None else summary
    if s.get("fleet_tool_ok") is None:
        if s.get("fleet_tool_unavailable_n"):
            return ("no evidence either way: the last %d fleet tool call(s) in %d minutes "
                    "could not run because the machine was not in a state to run them -- a "
                    "locked session or the secure desktop. Not a failure"
                    % (s["fleet_tool_unavailable_n"], int(FRESH_S // 60)))
        return ("no fleet tool calls in the last %d minutes -- no evidence either way, not a "
                "failure" % int(FRESH_S // 60))
    verb = "working" if s["fleet_tool_ok"] else "FAILING"
    return ("fleet tool path %s: %d ok, %d failed, last call %s %ss ago"
            % (verb, s["fleet_tool_ok_n"], s["fleet_tool_fail_n"],
               s.get("fleet_tool_last_tool"), s.get("fleet_tool_last_s")))
