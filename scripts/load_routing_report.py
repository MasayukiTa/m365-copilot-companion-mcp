# -*- coding: utf-8 -*-
"""Read-only report: from the shadow records of load-aware routing (C3), how often the router's
choice differs from the admission that happened, and whether there is enough data to decide.

WHAT DECISION THIS SERVES. Whether it is worth building a router that acts on the admission
margins and the role profile. relay/load_routing.py (setting `fanout_load_routing=shadow`)
appends one row per admission to .fleet/load_routing_shadow.jsonl; this script only reads those
rows (and the one rotated copy, `.1`) and writes nothing.

WHAT THE SHADOW CAN AND CANNOT SAY. It says how often a router would have chosen differently,
and in which margins (tight or loose). It cannot say the router would have done BETTER: the goal
it did not admit was not run in its place, so there is no outcome to compare. A switch-on
therefore needs a paired comparison of the two orders (roadmap C0), not this report alone.

TERMS
  admission    one row.
  had a choice the queue held another goal besides the one admitted (queue_len >= 2).
  comparable   the admitted goal's role AND at least one other queued goal's role both have a known
               expected cost, and those costs are not all equal. Only these rows can differ.
               A row where the admitted goal's role profile is unknown is not comparable: unknown
               is neither cheap nor expensive.
  differs      the router would have admitted another goal than the one admitted.

THE THRESHOLD FOR "ENOUGH DATA" (stated here, not buried in the logic). All four must hold:
  rows that had a choice   >= MIN_CHOICE_ROWS     (200)
  comparable rows          >= MIN_COMPARABLE      (30)
  distinct runs            >= MIN_RUNS            (5)   so one unusual run cannot be the answer
  roles with a known cost  >= MIN_KNOWN_ROLES     (2)   a single role can never be told apart
When they hold, the share of comparable rows that differ decides the next step:
  < DIFFER_WORTH (10%)  NOTHING_TO_GAIN: the router agrees with queue order almost always.
  >= 10%                WORTH_A_PAIRED_TEST: run the paired comparison (C0); do not switch on.
Anything else is INSUFFICIENT and the unmet requirements are listed.

  python scripts/load_routing_report.py [--fleet-dir .fleet] [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_NAME = "load_routing_shadow.jsonl"

MIN_CHOICE_ROWS = 200
MIN_COMPARABLE = 30
MIN_RUNS = 5
MIN_KNOWN_ROLES = 2
DIFFER_WORTH = 0.10

INSUFFICIENT, NOTHING_TO_GAIN, WORTH_A_PAIRED_TEST = (
    "INSUFFICIENT", "NOTHING_TO_GAIN", "WORTH_A_PAIRED_TEST")


def read_rows(path):
    """(rows, unreadable): the rotated copy first, then the live file. A missing file is empty;
    a line that is not a JSON object is counted, not guessed at."""
    rows, bad = [], 0
    for p in (path + ".1", path):
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
                    else:
                        bad += 1
        except OSError:
            pass
    return rows, bad


def _share(n, d):
    return (n / d) if d else None


def summarise(rows):
    had_choice = [r for r in rows if int(r.get("queue_len") or 0) >= 2]
    comparable = [r for r in had_choice if r.get("comparable")]
    differs = [r for r in comparable if r.get("differs")]
    by_margin = {}
    for label, want in (("tight", True), ("loose", False)):
        sub = [r for r in comparable if bool(r.get("tight")) == want]
        by_margin[label] = {"comparable": len(sub),
                            "differs": sum(1 for r in sub if r.get("differs"))}
    reasons = {}
    for r in rows:
        k = str(r.get("reason") or "")
        reasons[k] = reasons.get(k, 0) + 1
    roles = {}
    for r in rows:
        for c in r.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            e = roles.setdefault(str(c.get("role") or ""), {"seen": 0, "known_cost": False})
            e["seen"] += 1
            if c.get("turns") is not None:
                e["known_cost"] = True
    runs = {str(r.get("run_id") or "") for r in rows}
    runs.discard("")
    ts = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    return {"rows": len(rows), "had_choice": len(had_choice), "comparable": len(comparable),
            "differs": len(differs), "differ_share": _share(len(differs), len(comparable)),
            "by_margin": by_margin, "reasons": dict(sorted(reasons.items())),
            "roles": dict(sorted(roles.items())),
            "known_roles": sum(1 for e in roles.values() if e["known_cost"]),
            "runs": len(runs), "first_ts": min(ts) if ts else None,
            "last_ts": max(ts) if ts else None}


def verdict(s):
    """(label, [unmet requirements]) from a summary."""
    unmet = []
    if s["had_choice"] < MIN_CHOICE_ROWS:
        unmet.append("rows that had a choice: %d of %d" % (s["had_choice"], MIN_CHOICE_ROWS))
    if s["comparable"] < MIN_COMPARABLE:
        unmet.append("comparable rows: %d of %d" % (s["comparable"], MIN_COMPARABLE))
    if s["runs"] < MIN_RUNS:
        unmet.append("distinct runs: %d of %d" % (s["runs"], MIN_RUNS))
    if s["known_roles"] < MIN_KNOWN_ROLES:
        unmet.append("roles with a known cost: %d of %d" % (s["known_roles"], MIN_KNOWN_ROLES))
    if unmet:
        return INSUFFICIENT, unmet
    if (s["differ_share"] or 0.0) < DIFFER_WORTH:
        return NOTHING_TO_GAIN, []
    return WORTH_A_PAIRED_TEST, []


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def render(s, label, unmet, unreadable=0):
    lines = ["# Load-aware routing, shadow records", "",
             "Read-only. A shadow row is what the router would have chosen next to what the "
             "coordinator did; the goal it did not admit was not run, so nothing here shows the "
             "router would have done better.", "",
             "- rows: %d (unreadable lines skipped: %d); distinct runs: %d"
             % (s["rows"], unreadable, s["runs"]),
             "- rows that had a choice (queue held another goal): %d" % s["had_choice"],
             "- comparable rows (both costs known, not all equal): %d" % s["comparable"],
             "- would differ: %d (%s of comparable)" % (s["differs"], _pct(s["differ_share"])),
             "- tight margins: %d comparable, %d differ; loose margins: %d comparable, %d differ"
             % (s["by_margin"]["tight"]["comparable"], s["by_margin"]["tight"]["differs"],
                s["by_margin"]["loose"]["comparable"], s["by_margin"]["loose"]["differs"]),
             "- roles seen: %s; with a known cost: %d" % (
                 ", ".join("%s (%d)" % (k or "(none)", v["seen"]) for k, v in s["roles"].items())
                 or "none", s["known_roles"]), "", "## Reasons", ""]
    lines += ["- %s: %d" % (k or "(none)", v) for k, v in s["reasons"].items()] or ["none"]
    lines += ["", "## Verdict: %s" % label, ""]
    if unmet:
        lines.append("Not enough data to decide. Still needed:")
        lines += ["- " + u for u in unmet]
    elif label == NOTHING_TO_GAIN:
        lines.append("Enough data. The router agrees with queue order in almost every comparable "
                     "admission (under %d%% differ); a router that acts would change little."
                     % int(DIFFER_WORTH * 100))
    else:
        lines.append("Enough data. At least %d%% of comparable admissions would differ. Next step: "
                     "a paired comparison of the two orders (C0). Do not switch routing on from "
                     "shadow rows alone." % int(DIFFER_WORTH * 100))
    lines += ["", "Threshold: >= %d rows with a choice, >= %d comparable, >= %d runs, >= %d roles "
              "with a known cost." % (MIN_CHOICE_ROWS, MIN_COMPARABLE, MIN_RUNS, MIN_KNOWN_ROLES)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only load-aware routing shadow report.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    # Resolved here, not as a module constant: nothing in this script writes, and a module-level
    # path to the live record directory would have to be classified by the isolation guard.
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    rows, bad = read_rows(os.path.join(fleet_dir, LOG_NAME))
    s = summarise(rows)
    label, unmet = verdict(s)
    if args.format == "json":
        text = json.dumps({"summary": s, "verdict": label, "unmet": unmet, "unreadable": bad},
                          ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = render(s, label, unmet, bad)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
