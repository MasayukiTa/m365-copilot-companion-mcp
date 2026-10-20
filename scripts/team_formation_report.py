# -*- coding: utf-8 -*-
"""Read-only report: from the shadow records of team formation (C4), how often the proposed team
differs from the team the static split built, and whether there is enough data to decide.

WHAT DECISION THIS SERVES. Whether it is worth building a team-forming step that acts on the role
profile and the split's shape. relay/team_formation.py (setting `team_mode=shadow`) appends one
row per split to .fleet/team_formation_shadow.jsonl; this script only reads those rows (and the one
rotated copy, `.1`) and writes nothing.

WHAT THE SHADOW CAN AND CANNOT SAY. It says how often a rule would have proposed another team, and
which rule did it. It cannot say the other team would have done BETTER: it was never run, so there
is no outcome to compare. A switch-on needs a paired comparison of the two teams (roadmap C0).

TERMS
  split        one row (one campaign's split).
  differs      the proposed team (roles and counts) is not the static one.
  profile known  at least one of the roles `subtask` / `aggregator` had a success label other than
               unknown when the row was written. A row without it can only differ through the
               shape rule (fold_tiny); the review rule had nothing to go on.

THE THRESHOLD FOR "ENOUGH DATA" (stated here, not buried in the logic). All four must hold:
  splits                  >= MIN_SPLITS          (60)
  splits, profile known   >= MIN_PROFILE_KNOWN   (30)  so the profile-driven rule was exercised
  distinct runs           >= MIN_RUNS            (5)   so one unusual run cannot be the answer
  distinct shapes         >= MIN_SHAPES          (3)   (step counts), else every row is one case
When they hold, the share of splits that differ decides the next step:
  < DIFFER_WORTH (10%)  NOTHING_TO_GAIN: the rule agrees with the static team almost always.
  >= 10%                WORTH_A_PAIRED_TEST: run the paired comparison (C0); do not switch on.
Anything else is INSUFFICIENT and the unmet requirements are listed.

  python scripts/team_formation_report.py [--fleet-dir .fleet] [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_NAME = "team_formation_shadow.jsonl"

MIN_SPLITS = 60
MIN_PROFILE_KNOWN = 30
MIN_RUNS = 5
MIN_SHAPES = 3
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


def _steps(r):
    sh = r.get("shape")
    try:
        return int(sh.get("steps")) if isinstance(sh, dict) else None
    except (TypeError, ValueError):
        return None


def summarise(rows):
    differs = [r for r in rows if r.get("differs")]
    known = [r for r in rows if r.get("profile_known")]
    known_differs = [r for r in known if r.get("differs")]
    rules = {}
    for r in rows:
        for name in r.get("rules") or []:
            rules[str(name)] = rules.get(str(name), 0) + 1
    by_depth = {}
    for r in rows:
        e = by_depth.setdefault(str(r.get("depth") or 1), {"splits": 0, "differs": 0})
        e["splits"] += 1
        e["differs"] += 1 if r.get("differs") else 0
    shapes = {s for s in (_steps(r) for r in rows) if s is not None}
    runs = {str(r.get("run_id") or "") for r in rows}
    runs.discard("")
    delta = 0
    for r in differs:
        try:
            delta += int(r["proposed"]["members"]) - int(r["actual"]["members"])
        except (KeyError, TypeError, ValueError):
            pass
    ts = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    return {"splits": len(rows), "differs": len(differs), "differ_share": _share(len(differs), len(rows)),
            "profile_known": len(known), "profile_known_differs": len(known_differs),
            "rules": dict(sorted(rules.items())), "by_depth": dict(sorted(by_depth.items())),
            "shapes": len(shapes), "runs": len(runs), "members_added_total": delta,
            "first_ts": min(ts) if ts else None, "last_ts": max(ts) if ts else None}


def verdict(s):
    """(label, [unmet requirements]) from a summary."""
    unmet = []
    if s["splits"] < MIN_SPLITS:
        unmet.append("splits recorded: %d of %d" % (s["splits"], MIN_SPLITS))
    if s["profile_known"] < MIN_PROFILE_KNOWN:
        unmet.append("splits with a known profile: %d of %d" % (s["profile_known"], MIN_PROFILE_KNOWN))
    if s["runs"] < MIN_RUNS:
        unmet.append("distinct runs: %d of %d" % (s["runs"], MIN_RUNS))
    if s["shapes"] < MIN_SHAPES:
        unmet.append("distinct split shapes (step counts): %d of %d" % (s["shapes"], MIN_SHAPES))
    if unmet:
        return INSUFFICIENT, unmet
    if (s["differ_share"] or 0.0) < DIFFER_WORTH:
        return NOTHING_TO_GAIN, []
    return WORTH_A_PAIRED_TEST, []


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def render(s, label, unmet, unreadable=0):
    lines = ["# Team formation, shadow records", "",
             "Read-only. A shadow row is the team a rule would have proposed next to the static team "
             "the split built; the proposed team was never run, so nothing here shows it would have "
             "done better.", "",
             "- splits: %d (unreadable lines skipped: %d); distinct runs: %d; distinct shapes: %d"
             % (s["splits"], unreadable, s["runs"], s["shapes"]),
             "- proposed team differs: %d (%s of splits)" % (s["differs"], _pct(s["differ_share"])),
             "- splits with a known profile: %d, of which differ: %d"
             % (s["profile_known"], s["profile_known_differs"]),
             "- members added (+) or removed (-) across the differing proposals: %+d"
             % s["members_added_total"], "", "## Rules fired", ""]
    lines += ["- %s: %d" % (k, v) for k, v in s["rules"].items()] or ["none"]
    lines += ["", "## By depth", ""]
    lines += ["- depth %s: %d splits, %d differ" % (k, v["splits"], v["differs"])
              for k, v in s["by_depth"].items()] or ["none"]
    lines += ["", "## Verdict: %s" % label, ""]
    if unmet:
        lines.append("Not enough data to decide. Still needed:")
        lines += ["- " + u for u in unmet]
    elif label == NOTHING_TO_GAIN:
        lines.append("Enough data. The rule agrees with the static team in almost every split "
                     "(under %d%% differ); a team-forming step that acts would change little."
                     % int(DIFFER_WORTH * 100))
    else:
        lines.append("Enough data. At least %d%% of splits would get another team. Next step: a "
                     "paired comparison of the two teams (C0). Do not switch team formation on "
                     "from shadow rows alone." % int(DIFFER_WORTH * 100))
    lines += ["", "Threshold: >= %d splits, >= %d with a known profile, >= %d runs, >= %d distinct "
              "shapes." % (MIN_SPLITS, MIN_PROFILE_KNOWN, MIN_RUNS, MIN_SHAPES)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only team formation shadow report.")
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
