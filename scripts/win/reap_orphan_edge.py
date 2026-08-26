"""Stop a managed Edge whose owner is gone.

WHY THIS EXISTS. The measurement series launches its own browser on :9224 and has no code
path that ever stops it -- no teardown, no atexit, nothing. When the series ended (or was
interrupted) the browser was simply orphaned: no keepalive supervised it and no reaper
collected it, so it sat holding a Copilot chat tab. Found on 2026-08-26 idling at 331 MB,
hours after the run that started it had finished, with a `チャット | Microsoft Copilot` tab
nobody was reading.

Adding a teardown to the measurement script is the first fix, but a teardown only runs when
the script gets to run it. A hard kill, a machine that loses power, a terminal closed on a
long run -- each leaves the browser behind again. So ownership is checked here instead, from
outside, against the one thing that cannot lie: the process table.

OWNERSHIP. Each managed profile exists to serve exactly one kind of process:

    copilot-eval-edge       the measurement series          run_transport_series / diag_warmup_bias
    copilot-companion-edge  a fleet run                     fleet_runner
    copilot-bridge-edge     the bridge                      copilot_bridge

If no process of the owning kind is running, that browser has nothing to serve, and every
byte it holds is waste. That rule is derived entirely from the process table -- no marker
files, no .fleet state -- so it gives the same answer on a machine that has never run this
stack before, and in a worktree with no prior state. A rule that needs a file some other
device does not have is not a guarantee.

THE BRIDGE IS DELIBERATELY EXEMPT FROM THE DEFAULT. Its Edge is supervised: start_bridge.ps1
brings it back whenever CDP stops answering, so reaping it here would fight that supervisor
rather than reclaim anything. Pass --include-bridge to reap it anyway (useful when the
supervisor itself is gone).

    python scripts/win/reap_orphan_edge.py            # report only
    python scripts/win/reap_orphan_edge.py --stop     # stop the orphans
"""
from __future__ import annotations

import argparse
import subprocess
import sys

#: profile -> (human name, regex matching the command line of a process that owns it)
OWNERS = {
    "copilot-eval-edge": ("the measurement series", r"run_transport_series|diag_warmup_bias"),
    "copilot-companion-edge": ("a fleet run", r"fleet_runner"),
    "copilot-bridge-edge": ("the bridge", r"copilot_bridge"),
}

#: Reaped only when asked for: this one has a supervisor that would put it straight back.
SUPERVISED = ("copilot-bridge-edge",)


def _ps(script, timeout=40):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout or ""
    except Exception:
        return ""


def owner_alive(pattern):
    """Is any process whose command line matches `pattern` running?

    python.exe AND powershell.exe: a series driven from a .ps1 wrapper is just as much an
    owner as one started directly, and missing that would reap a browser out from under a
    live run -- the one mistake this script must never make.
    """
    script = ("@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR "
              "Name='powershell.exe' OR Name='pwsh.exe'\" | "
              "Where-Object { $_.CommandLine -match '%s' }).Count" % pattern)
    out = _ps(script).strip()
    return out.isdigit() and int(out) > 0


def browser_procs(profile):
    """(count, total MB) for every Edge process on `profile`, children included."""
    script = ("$p = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -match '%s' }; "
              "\"{0} {1}\" -f @($p).Count, [int](($p | "
              "Measure-Object WorkingSetSize -Sum).Sum / 1MB)" % profile)
    parts = _ps(script).split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].lstrip("-").isdigit():
        return int(parts[0]), int(parts[1])
    return 0, 0


def stop_profile(profile):
    """Stop every Edge process on this profile. Returns how many were asked to stop."""
    n, _ = browser_procs(profile)
    if n:
        _ps("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -match '%s' } | "
            "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } "
            "catch {} }" % profile, timeout=60)
    return n


def survey(include_bridge=False):
    """What is running, who owns it, and is that owner alive. Pure reporting."""
    rows = []
    for profile, (who, pattern) in OWNERS.items():
        procs, mb = browser_procs(profile)
        if not procs:
            continue
        exempt = profile in SUPERVISED and not include_bridge
        alive = owner_alive(pattern)
        rows.append({"profile": profile, "owner": who, "procs": procs, "mb": mb,
                     "owner_alive": alive, "exempt": exempt,
                     "orphan": (not alive) and not exempt})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stop", action="store_true", help="stop the orphans (default: report)")
    ap.add_argument("--include-bridge", action="store_true",
                    help="also consider the bridge's Edge, which is normally supervised")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    rows = survey(include_bridge=args.include_bridge)
    if not rows:
        print("no managed Edge is running.")
        return 0

    reclaimable = 0
    for r in rows:
        if r["exempt"]:
            state = "exempt (supervised)"
        elif r["owner_alive"]:
            state = "in use by %s" % r["owner"]
        else:
            state = "ORPHAN -- %s is not running" % r["owner"]
            reclaimable += r["mb"]
        print("  %-24s %2d proc %6d MB  %s" % (r["profile"], r["procs"], r["mb"], state))

    orphans = [r for r in rows if r["orphan"]]
    if not orphans:
        print("\nNothing to reap.")
        return 0
    if not args.stop:
        print("\n%d orphan(s) holding %d MB. Re-run with --stop to reclaim it."
              % (len(orphans), reclaimable))
        return 0

    print("")
    for r in orphans:
        n = stop_profile(r["profile"])
        print("  stopped %s (%d process(es), %d MB)" % (r["profile"], n, r["mb"]))
    print("\nreclaimed roughly %d MB." % reclaimable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
