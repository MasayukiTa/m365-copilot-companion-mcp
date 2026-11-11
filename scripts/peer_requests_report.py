# -*- coding: utf-8 -*-
"""Read-only report: from the shadow records of peer work requests (roadmap C2), how often a
peer REQUEST and a matching OFFER co-occurred (a CLAIM could have been granted), and whether
there is enough data to decide.

WHAT DECISION THIS SERVES. Whether it is worth promoting the three peer kinds (REQUEST/OFFER/
CLAIM) onto the agent board and letting the coordinator grant a CLAIM inside the A3 budget.
relay/peer_requests.py (setting `peer_requests=shadow`) appends one row per coordinator tick that
had any action to .fleet/peer_requests_shadow.jsonl; this script only reads those rows (and the
one rotated copy, `.1`) and writes nothing.

WHAT THE SHADOW CAN AND CANNOT SAY. It says how often a peer protocol WOULD have had a request to
serve and a free worker to serve it. It cannot say the claim would have FINISHED the work sooner:
nothing was ever posted or reassigned, so there is no outcome to compare. A switch-on needs the
kinds on the board behind their own review, and a paired comparison.

TERMS
  tick          one recorded coordinator tick (one row; only ticks with an action are written).
  claimable     a tick whose REQUEST and OFFER co-occur, so a CLAIM could have been granted.

THE THRESHOLD FOR "ENOUGH DATA" (stated here, not buried in the logic). All three must hold:
  ticks                 >= MIN_TICKS   (60)
  distinct runs         >= MIN_RUNS    (5)   so one unusual run cannot be the answer
  ticks with a request  >= MIN_REQUEST (20)  so the request side was actually exercised
When they hold, the share of recorded ticks that are claimable decides the next step:
  < CLAIM_WORTH (10%)   NOTHING_TO_GAIN: a free worker almost never lines up with a request.
  >= 10%                WORTH_BOARD_PROMOTION: put the kinds on the board behind review (C2).
Anything else is INSUFFICIENT and the unmet requirements are listed.

  python scripts/peer_requests_report.py [--fleet-dir .fleet] [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_NAME = "peer_requests_shadow.jsonl"

MIN_TICKS = 60
MIN_RUNS = 5
MIN_REQUEST = 20
CLAIM_WORTH = 0.10

INSUFFICIENT, NOTHING_TO_GAIN, WORTH_BOARD_PROMOTION = (
    "INSUFFICIENT", "NOTHING_TO_GAIN", "WORTH_BOARD_PROMOTION")


def read_rows(path):
    """(rows, unreadable): the rotated copy first, then the live file. A missing file is not an
    error (nothing has been recorded yet); a malformed line is counted in `unreadable`."""
    rows, unreadable = [], 0
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        unreadable += 1
        except OSError:
            pass
    return rows, unreadable


def summarize(rows):
    """Pure: counts, the claimable share, the verdict and any unmet requirements."""
    ticks = len(rows)
    runs = {str(r.get("run_id") or "") for r in rows}
    with_request = sum(1 for r in rows if (r.get("counts") or {}).get("request"))
    with_offer = sum(1 for r in rows if (r.get("counts") or {}).get("offer"))
    claimable = sum(1 for r in rows if (r.get("counts") or {}).get("claim_work"))
    requests = sum(int((r.get("counts") or {}).get("request") or 0) for r in rows)
    offers = sum(int((r.get("counts") or {}).get("offer") or 0) for r in rows)
    claims = sum(int((r.get("counts") or {}).get("claim_work") or 0) for r in rows)
    share = (claimable / ticks) if ticks else 0.0

    unmet = []
    if ticks < MIN_TICKS:
        unmet.append("ticks %d < %d" % (ticks, MIN_TICKS))
    if len(runs) < MIN_RUNS:
        unmet.append("distinct runs %d < %d" % (len(runs), MIN_RUNS))
    if with_request < MIN_REQUEST:
        unmet.append("ticks with a request %d < %d" % (with_request, MIN_REQUEST))

    if unmet:
        verdict = INSUFFICIENT
    elif share < CLAIM_WORTH:
        verdict = NOTHING_TO_GAIN
    else:
        verdict = WORTH_BOARD_PROMOTION

    return {
        "ticks": ticks, "runs": len(runs),
        "ticks_with_request": with_request, "ticks_with_offer": with_offer,
        "claimable_ticks": claimable, "claimable_share": round(share, 4),
        "requests": requests, "offers": offers, "claims": claims,
        "verdict": verdict, "unmet": unmet,
    }


def format_text(s):
    lines = [
        "peer work requests (shadow) -- roadmap C2",
        "  recorded ticks     : %d  (across %d runs)" % (s["ticks"], s["runs"]),
        "  ticks with request : %d" % s["ticks_with_request"],
        "  ticks with offer   : %d" % s["ticks_with_offer"],
        "  claimable ticks    : %d  (%.1f%% of recorded)" % (
            s["claimable_ticks"], 100.0 * s["claimable_share"]),
        "  totals             : %d requests, %d offers, %d claims" % (
            s["requests"], s["offers"], s["claims"]),
        "  verdict            : %s" % s["verdict"],
    ]
    if s["unmet"]:
        lines.append("  not enough data    : " + "; ".join(s["unmet"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only peer work requests shadow report.")
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    rows, unreadable = read_rows(os.path.join(args.fleet_dir, LOG_NAME))
    s = summarize(rows)
    s["unreadable_lines"] = unreadable
    if args.format == "json":
        print(json.dumps(s, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(format_text(s))
        if unreadable:
            print("  (%d unreadable line(s) skipped)" % unreadable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
