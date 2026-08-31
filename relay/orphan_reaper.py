"""Kill the build processes a finished worker left behind.

WHY. On 2026-08-30 seventeen npx processes -- tsc, jest, mocha -- were still running fourteen
hours after the runs that started them ended. They held 672 MB of RAM and, worse, they held
the worktree files open: `git worktree remove` failed, the capture step left husks that
resolve to the harness's own repository, and the free-disk figure that the fleet admits work
against was wrong by the size of six checkouts.

WHAT MAKES IT SAFE TO KILL SOMETHING. Only processes this fleet started, identified by their
working directory being inside the run's own work tree, and only when they are older than a
threshold no live build reaches. The rule the operator states is not "no killing" but "do not
touch a process you did not start", so the test is provenance, never resource usage: a
memory-hungry process that belongs to somebody else is not this module's business.

It reports what it would kill before killing anything, because a reaper nobody can audit is
the kind that eventually takes the wrong process.
"""
from __future__ import annotations

import os
import subprocess
import time

#: Nothing a build legitimately does runs this long after its fleet has gone idle. Measured
#: against the incident: the survivors were 7 to 14 hours old.
DEFAULT_MIN_AGE_S = 2 * 3600


def _work_root():
    return os.path.normcase(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".fleet", "swe", "work"))


def candidates(min_age_s=DEFAULT_MIN_AGE_S, work_root=None):
    """[(pid, age_s, cmd)] for processes this fleet started and left behind.

    Attribution is by ANCESTRY as well as by command line. A build tool's own arguments say
    nothing about who launched it -- `npx tsc` looks the same whoever ran it -- and Windows
    exposes no cheap cwd for another process, so matching the work root against the command
    line was the only test. It attributed the worker's own shell and nothing it spawned.

    Measured 2026-08-31: eight `npx eslint src/user/settings.js ...` processes from runs that
    had finished the previous evening were still alive, with 545 MB of npm cache behind them.
    Their command lines carry relative paths, so the work root was nowhere in them and the
    reaper left every one alone -- exactly as its own docstring said it would.

    Walking ParentProcessId fixes that without any new dependency: a process whose ANCESTOR
    was attributed is attributed too. A child must be no older than the ancestor it is claimed
    by, which keeps a reused pid from adopting an unrelated process.
    """
    root = os.path.normcase(work_root or _work_root())
    out = []
    ps = ("Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | "
          "ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        import json
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return out
    if isinstance(rows, dict):
        rows = [rows]
    now = time.time()

    def _start(p):
        try:
            return time.mktime(time.strptime(str(p.get("CreationDate") or "")[:14],
                                             "%Y%m%d%H%M%S"))
        except Exception:
            return None

    by_pid = {}
    for p in rows:
        try:
            by_pid[int(p.get("ProcessId"))] = p
        except Exception:
            continue

    # Directly attributable: the work root appears in the command line.
    attributed = {}
    for pid, p in by_pid.items():
        if root in os.path.normcase(p.get("CommandLine") or ""):
            st = _start(p)
            if st is not None:
                attributed[pid] = st

    # Then everything descended from one of those. A child must not predate the ancestor that
    # claims it: pids are reused, and an older process sharing a recycled parent id is not a
    # descendant of anything.
    changed = True
    while changed:
        changed = False
        for pid, p in by_pid.items():
            if pid in attributed:
                continue
            try:
                ppid = int(p.get("ParentProcessId") or 0)
            except Exception:
                continue
            anc = attributed.get(ppid)
            if anc is None:
                continue
            st = _start(p)
            if st is None or st < anc:
                continue
            attributed[pid] = st
            changed = True

    for pid, st in attributed.items():
        age = now - st
        if age >= min_age_s:
            out.append((pid, int(age), (by_pid[pid].get("CommandLine") or "")[:160]))
    return out


def reap(min_age_s=DEFAULT_MIN_AGE_S, dry_run=True, work_root=None):
    """Report, and kill only when asked. Never raises."""
    found = candidates(min_age_s, work_root)
    killed = []
    if not dry_run:
        for pid, _age, _cmd in found:
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid],
                               capture_output=True, timeout=60)
                killed.append(pid)
            except Exception:
                pass
    return {
        "work_root": os.path.normcase(work_root or _work_root()),
        "min_age_s": min_age_s,
        "found": [{"pid": p, "age_s": a, "cmd": c} for p, a, c in found],
        "killed": killed,
        "dry_run": dry_run,
        "rule": ("provenance, not resource usage: only processes whose command line names "
                 "this run's work tree, and only when older than the threshold. A process "
                 "somebody else started is not this module's business however large it is."),
    }
