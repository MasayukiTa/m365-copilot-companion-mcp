"""Relaunch a fleet run whose coordinator died, using the decision that already existed.

WHY THIS EXISTS. Everything needed was in place and nothing joined it up. fleet_run_active.json
is written once at run start and cleared on clean completion or an explicit stop, so a marker
with a DEAD pid is exactly an interrupted run. `resume_argv` is precomputed into that marker
with the goal flags stripped, ready for a `--resume` relaunch. `should_auto_resume()` states
the rule and is covered by tests. Its docstring says scripts/supervisor.ps1 mirrors the rule;
that script supervises the MCP server and the dev tunnel and never mentions the marker.

So an interrupted run stayed interrupted. The goals were recoverable the whole time -- the
ledger reconstructs them -- and the way anyone found out was by noticing the answer never came.

ORDER MATTERS AGAINST THE REAPER. relay.fleet_reaper finalises the sidecars of a dead run and
REMOVES the marker, which is the same file this reads to know there is anything to resume. Run
this first: a resumed run writes a fresh marker with a live pid, and the reaper then correctly
leaves it alone.

    python scripts/win/resume_interrupted_fleet.py            # report only
    python scripts/win/resume_interrupted_fleet.py --resume   # actually relaunch
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.fleet_runner import ACTIVE_MARKER, should_auto_resume   # noqa: E402


def read_marker(state_dir: str):
    """The marker as written at run start, or None. utf-8-sig, like the other readers here."""
    try:
        with open(os.path.join(state_dir, ACTIVE_MARKER), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("pid") else None
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    """Is that process still running?

    Decided from the process table, never from the marker: the marker is written once and
    says nothing about whether its process survived, which is the whole question. On any
    doubt the answer is True -- relaunching a run that is actually alive would put two
    coordinators on one state directory, and they would overwrite each other's status.
    """
    script = "@(Get-CimInstance Win32_Process -Filter \"ProcessId=%s\").Count" % int(pid)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=25).stdout.strip()
        return not (out.isdigit() and int(out) == 0)
    except Exception:
        return True


def resume_command(marker: dict) -> list:
    """The argv that continues the interrupted run, or [] when the marker cannot supply one.

    `--resume` alone rebuilds the goal set from the durable ledger. Replaying the original
    --goals-file on top of it would add every goal a second time, finished ones included,
    which is why the marker stores an argv with those flags already stripped.
    """
    argv = marker.get("resume_argv")
    if not isinstance(argv, list) or not argv:
        return []
    return [sys.executable, "-m", "relay.fleet_runner"] + [str(a) for a in argv] + ["--resume"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--resume", action="store_true",
                    help="relaunch it (default: report what would happen)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    marker = read_marker(args.state_dir)
    if not marker:
        print("no interrupted fleet run (no active-run marker).")
        return 0

    pid = marker.get("pid")
    alive = pid_alive(pid)
    if not should_auto_resume(True, alive):
        print("a run is live (pid %s) -- nothing to resume." % pid)
        return 0

    command = resume_command(marker)
    if not command:
        print("pid %s is dead, but the marker carries no resume_argv -- cannot continue it "
              "automatically. Its goals are in the ledger (last_run_goals.json)." % pid)
        return 1

    if not args.resume:
        print("pid %s is dead. Would resume with:\n  %s" % (pid, " ".join(command[1:])))
        print("run again with --resume to do it.")
        return 0

    print("resuming the run interrupted at pid %s ..." % pid)
    try:
        subprocess.Popen(command, cwd=REPO,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        print("could not relaunch: %s: %s" % (type(exc).__name__, exc))
        return 1
    print("relaunched. It reconstructs the unfinished goals from the ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
