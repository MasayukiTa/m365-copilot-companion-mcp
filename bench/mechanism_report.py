"""Where each accuracy mechanism stops, from telemetry rather than from reconstruction.

THE QUESTION THIS ANSWERS. Measuring usage after the fact could not tell "never configured on"
from "on and the situation never arose" from "fired and changed nothing". That ambiguity is
what let a stack of accuracy machinery go unexamined for a long time: the multi-lens panel ran
on 4.3% of panels and the security lens seven times ever, and neither number said which of the
three it was.

Run this DURING a benchmark, not after it. The point of the staircase is to see a mechanism
fail to reach its situation while there is still a run to change.
"""
from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay.mechanism_telemetry import LOG, funnel, load


def render(rows):
    f = funnel(rows)
    lines = ["%-10s %8s %10s %8s %9s %8s %8s   %s"
             % ("mechanism", "records", "configured", "eligible", "triggered",
                "executed", "changed", "stops at")]
    lines.append("-" * 96)
    for m, v in f.items():
        lines.append("%-10s %8d %10d %8d %9d %8d %8d   %s"
                     % (m, v["records"], v["configured"], v["eligible"], v["triggered"],
                        v["executed"], v["changed_decision"], v["stops_at"]))
    lines.append("")
    lines.append("A mechanism at 'never configured' was not given a chance -- that is a")
    lines.append("deployment fact, not evidence about the idea. One at 'no opportunity' is")
    lines.append("solving a problem this workload does not present. One at 'changed nothing'")
    lines.append("ran and made no difference, which is the only one of the three that is")
    lines.append("evidence about the mechanism itself -- and even then, a changed decision is")
    lines.append("not a better one until a grader says so.")
    return "\n".join(lines)


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = load(a.log)
    if a.json:
        print(json.dumps({"records": len(rows), "funnel": funnel(rows)},
                         indent=2, ensure_ascii=False))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
