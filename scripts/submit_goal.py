# -*- coding: utf-8 -*-
"""Put a goal on the fleet from the command line, visibly.

WHY THIS EXISTS RATHER THAN `fleet_runner.py -g`. Launching the runner IS NOT SUBMITTING. The
runner is the thing that runs; using it as a submission channel is what made a CLI submission
invisible, because it writes nothing until the run is already under way:

  * no queue entry, so the cockpit cannot show it -- and the rule in this project is that work
    which cannot be confirmed in the GUI does not count as working;
  * no history row;
  * and if the process dies before argparse -- a bad path, the wrong interpreter, a failed
    import -- not even a coordinator log. From the screen and from every record on disk, a
    submission that failed early is indistinguishable from a command nobody typed.

Reported 2026-09-18: a goal went in through the CLI, was not on the fleet, was not in the
history, and could not be found anywhere.

WHAT THIS DOES INSTEAD. It calls tools.fleet_intake.fleet_submit, which writes
.fleet/tasks/pending/<id>.json BEFORE anything runs. The cockpit reads that directory, so the
job is on screen the moment this returns -- measured at two seconds -- and a run that never
starts leaves the entry there, which is the difference between "refused" and "never happened".

The runner then picks it up the way it picks up any queued job. Nothing here starts a fleet.

    python scripts/submit_goal.py "the whole instruction, standalone"
    python scripts/submit_goal.py --source "night shift" --note "for the 3am sweep" "..."
    python scripts/submit_goal.py --file goals.txt        # one goal per line, # comments ok

`--source` is not decoration: it travels with the job as origin.source and is the only record
of where an instruction came from. A job submitted by an agent over the tunnel is not the same
authority as one a person typed, and the consumer is entitled to treat them differently. It
defaults to the invoking user and host rather than to nothing.
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import platform
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _default_source():
    """Who ran this, as far as the machine can say. Never raises."""
    try:
        return "cli %s@%s" % (getpass.getuser(), platform.node())
    except Exception:
        return "cli"


def _goals_from_file(path):
    """One goal per line; blank lines and # comments dropped."""
    out = []
    for raw in io.open(path, encoding="utf-8-sig").read().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Queue a goal for this machine's fleet. Queues; does not start anything.")
    ap.add_argument("goal", nargs="*", help="the whole instruction, standalone")
    ap.add_argument("--file", help="read goals from a file, one per line (# comments ok)")
    ap.add_argument("--source", default="", help="where the instruction came from")
    ap.add_argument("--note", default="", help="context for the human reviewing the queue")
    args = ap.parse_args(argv)

    goals = list(args.goal or [])
    if args.file:
        try:
            goals.extend(_goals_from_file(args.file))
        except OSError as exc:
            print("could not read %s: %s" % (args.file, exc), file=sys.stderr)
            return 2
    # JOINED, NOT TREATED AS SEPARATE GOALS. A shell that splits an unquoted instruction into
    # words would otherwise queue one job per word -- and fleet_submit's duplicate guard would
    # not catch it, because the fragments differ.
    if len(goals) > 1 and not args.file:
        goals = [" ".join(goals)]
    goals = [g for g in (g.strip() for g in goals) if g]
    if not goals:
        print("nothing to submit: pass the instruction as an argument, or --file", file=sys.stderr)
        return 2

    from tools.fleet_intake import fleet_submit

    source = args.source.strip() or _default_source()
    failed = 0
    for g in goals:
        out = fleet_submit(goal=g, note=args.note, source=source)
        print(out)
        # fleet_submit reports a refusal in its return string rather than by raising, and a
        # refusal is the answer -- an empty queue is not the same as a queue that said no.
        if out.startswith("[fleet_submit:"):
            failed += 1
    if failed:
        print("%d of %d refused -- read the message above; nothing was started either way"
              % (failed, len(goals)), file=sys.stderr)
        return 1
    print("queued, not started. The cockpit shows it now; it runs when a coordinator picks "
          "it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
