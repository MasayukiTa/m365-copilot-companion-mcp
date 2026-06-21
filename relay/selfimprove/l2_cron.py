"""L2 scheduler entrypoint: run ONE gated self-improvement iteration, unattended.

This is the cron-facing rung of L2 (bench/SELF_GROWTH_L4_DESIGN.md sec 1: "CronCreate schedules
iterations; auto-commits ONLY changes that pass the gate AND leave the frozen set intact"). L2 itself
(relay/selfimprove/l2.py) already wraps one iteration in the constitutional discipline. This module
adds the parts a scheduler needs:

  - a single-instance lock (`IterationLock`) so a slow iteration is never run twice concurrently when
    the schedule fires again before the last one finished;
  - `run_once(...)`, the gated single shot, ordered LOCK -> FROZEN (sec 0) -> SPEND CEILING (sec 8) ->
    DRY-RUN -> REAL, that NEVER raises and ALWAYS releases the lock;
  - `cron_command(...)`, the exact command string an operator registers with Task Scheduler / cron.

SAFETY -- this module is MEASUREMENT-SAFE by default. l2.run_iteration ultimately drives the fleet (a
real solve), so `run_once` defaults to `dry_run=True`: it runs every gate but does NOT call the
iteration. The CLI default is also `--dry-run`. A real run is an explicit operator opt-in (`--run`),
intended for AFTER any live measurement has finished. Tests inject a stub iterate_fn and never touch
the real one. This module does NOT register any schedule -- that is a separate, deliberate operator
step (see `cron_command` docstring).

  python -m relay.selfimprove.l2_cron               # safe: gates only (dry-run)
  python -m relay.selfimprove.l2_cron --print-cron  # print the scheduler command line and exit
  python -m relay.selfimprove.l2_cron --run         # POST-MEASUREMENT: drives the fleet
  python -m relay.selfimprove.test_l2_cron          # hermetic tests (no real solve, no git, no net)
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Optional

REPO = r"C:\Users\USER\companion-mcp"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.selfimprove import frozen as F
from relay.selfimprove import guards as G
from relay.selfimprove import l2 as L2

DEFAULT_LOCK = os.path.join(REPO, ".fleet", "selfimprove", "l2_cron.lock")

# The cmdline marker a live iteration process carries; used by the default liveness check so a stale
# lock left by a dead pid is reclaimable but a lock held by a running iteration is not.
_LIVE_MARKER = "relay.selfimprove.l2_cron"


# --------------------------------------------------------------------------------------------------
# Default pid-liveness check (injectable -- tests pass their own is_alive_fn)
# --------------------------------------------------------------------------------------------------

def _default_is_alive(pid: int) -> bool:
    """True iff a process with this pid is alive AND looks like an l2_cron iteration.

    Liveness is intentionally conservative: we only treat the lock as live if the recorded pid is
    both running and carries the l2_cron marker in its command line (via guards.proc_alive, the
    cmdline-based check learned the hard way this session -- a bare pid check on a venv shim lies).
    Any pid that is gone, or running something else, is treated as a stale/reclaimable lock.
    """
    try:
        import psutil
        try:
            p = psutil.Process(int(pid))
            if not p.is_running():
                return False
            cl = " ".join(p.cmdline() or [])
            return _LIVE_MARKER in cl
        except Exception:
            return False
    except Exception:
        # psutil unavailable: fall back to "is any l2_cron process alive at all?". This errs toward
        # treating the lock as live (safer: avoids a second concurrent real iteration).
        return G.proc_alive(_LIVE_MARKER) >= 1


# --------------------------------------------------------------------------------------------------
# Single-instance lock
# --------------------------------------------------------------------------------------------------

class IterationLock:
    """File-based single-instance lock for the L2 cron iteration.

    The lock file stores the owning pid (one integer, text). `acquire()` returns True and writes the
    current pid only if no LIVE lock already exists; a lock whose recorded pid is dead (per
    `is_alive_fn`) is STALE and reclaimable. `release()` removes the file iff this instance owns it.
    Liveness is injectable (`is_alive_fn`) so tests are deterministic; it defaults to a real
    psutil/proc check. Usable as a context manager: `with IterationLock() as lk: if lk.acquired: ...`.
    """

    def __init__(self, path: str = DEFAULT_LOCK, *,
                 is_alive_fn: Optional[Callable[[int], bool]] = None,
                 pid: Optional[int] = None):
        self.path = path
        self._is_alive = is_alive_fn or _default_is_alive
        self._pid = int(pid) if pid is not None else os.getpid()
        self.acquired = False

    def _read_pid(self) -> Optional[int]:
        """Return the pid recorded in the lock file, or None if absent/unreadable/garbage."""
        try:
            with open(self.path, encoding="utf-8") as f:
                txt = f.read().strip()
            return int(txt) if txt else None
        except Exception:
            return None

    def acquire(self) -> bool:
        """Take the lock. False if a LIVE lock is held by another process; True (and write pid) else.

        A stale lock (recorded pid not alive, or missing/garbage) is reclaimed. Re-acquiring while we
        already hold it returns True (idempotent).
        """
        if self.acquired:
            return True
        existing = self._read_pid()
        if existing is not None and existing != self._pid and self._is_alive(existing):
            return False  # a live iteration holds the lock
        # free, stale, or already ours -> claim it
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write(str(self._pid) + "\n")
        self.acquired = True
        return True

    def release(self) -> None:
        """Remove the lock file iff we currently hold it. Never raises."""
        if not self.acquired:
            return
        try:
            if self._read_pid() == self._pid:
                os.remove(self.path)
        except Exception:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> "IterationLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# --------------------------------------------------------------------------------------------------
# The gated single shot
# --------------------------------------------------------------------------------------------------

def run_once(*, toggle: str = "SWE_MISS85_DISCIPLINE", n: int = 200, dataset_key: str = "Verified",
             auto_commit: bool = False, dry_run: bool = True,
             lock_path: Optional[str] = None, baseline_path: Optional[str] = None,
             spend: Optional["L2.SpendCeiling"] = None,
             max_iters: Optional[int] = None, max_hours: Optional[float] = None,
             now_fn: Optional[Callable[[], float]] = None,
             iterate_fn: Optional[Callable[..., dict]] = None,
             is_alive_fn: Optional[Callable[[int], bool]] = None) -> dict:
    """Run ONE gated iteration (or, by default, just prove the gates pass) and return a status dict.

    Gate order -- each is a tripwire; the first failure short-circuits and the lock is released:

      a. LOCK   -- acquire IterationLock(lock_path). If another LIVE iteration holds it -> status
                   "skipped" (do nothing; the running one is in charge).
      b. FROZEN -- frozen.frozen_intact(baseline_path) (sec 0). If changed -> status "abort".
      c. CEILING-- if a SpendCeiling is given and it is exceeded (sec 8) -> status "ceiling".
      d. DRY-RUN-- if dry_run (the default, measurement-safe) -> status "dry_run" WITHOUT calling
                   iterate_fn. The gates passed; a real run *would* proceed.
      e. REAL   -- else call iterate_fn (default l2.run_iteration) with toggle/n/dataset_key/
                   auto_commit/baseline_path, tick the ceiling if given -> status "ran".

    The lock is ALWAYS released (finally). run_once NEVER raises: any unexpected error is caught and
    returned as status "error". now_fn / is_alive_fn / iterate_fn are injectable for deterministic
    tests; outside tests they default to a real clock, a real pid check, and l2.run_iteration.
    """
    lock = IterationLock(lock_path or DEFAULT_LOCK, is_alive_fn=is_alive_fn)
    try:
        # ---- (a) LOCK -------------------------------------------------------------------------
        if not lock.acquire():
            return {"status": "skipped", "reason": "another iteration is running"}

        # ---- (b) FROZEN -----------------------------------------------------------------------
        baseline = baseline_path or F.DEFAULT_BASELINE
        ok, changed = F.frozen_intact(baseline_path=baseline)
        if not ok:
            return {"status": "abort",
                    "reason": "frozen set changed: %s" % ", ".join(changed),
                    "frozen_ok": False}

        # ---- (c) SPEND CEILING ----------------------------------------------------------------
        if spend is not None:
            now_ts = now_fn() if now_fn is not None else time.time()
            if spend.exceeded(max_iters, max_hours, now_ts):
                return {"status": "ceiling",
                        "reason": ("spend ceiling reached (iters=%d, max_iters=%s, max_hours=%s)"
                                   % (spend.iters, max_iters, max_hours)),
                        "frozen_ok": True}

        # ---- (d) DRY-RUN (default, measurement-safe) ------------------------------------------
        if dry_run:
            return {"status": "dry_run",
                    "reason": "gates passed; would run one iteration",
                    "frozen_ok": True}

        # ---- (e) REAL -------------------------------------------------------------------------
        run = iterate_fn if iterate_fn is not None else L2.run_iteration
        result = run(toggle=toggle, n=n, dataset_key=dataset_key,
                     auto_commit=auto_commit, baseline_path=baseline)
        if spend is not None:
            spend.tick()
        return {"status": "ran", "result": result}

    except Exception as e:  # never raise out of run_once
        return {"status": "error", "reason": str(e)}
    finally:
        lock.release()


# --------------------------------------------------------------------------------------------------
# Scheduler command-line helper (string only -- does NOT register anything)
# --------------------------------------------------------------------------------------------------

def cron_command(*, python: Optional[str] = None, dry_run: bool = False) -> str:
    """Return the exact command line a scheduler should invoke to run one iteration.

    This is a STRING helper ONLY. It does NOT create or register a Windows Scheduled Task / cron
    entry -- registering the schedule is a separate, deliberate, post-measurement operator step (use
    Task Scheduler or the scheduled-tasks tooling, pointing the action at this exact string). Keeping
    registration out of code means merely importing or running this module can never silently arm an
    unattended fleet-driving loop.

    dry_run=True yields the safe `--dry-run` form (gates only); dry_run=False yields `--run` (the
    fleet-driving form). The interpreter defaults to the repo venv python.
    """
    py = python or os.path.join(REPO, ".venv", "Scripts", "python.exe")
    flag = "--dry-run" if dry_run else "--run"
    return '"%s" -m relay.selfimprove.l2_cron %s' % (py, flag)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

def _main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--print-cron" in argv:
        print(cron_command())
        return 0

    real = "--run" in argv
    # default and --dry-run are both the safe path
    dry = not real

    try:
        if real:
            print("NOTE: --run drives the FLEET (a real solve). Use this only post-measurement.")
        res = run_once(dry_run=dry)
        status = res.get("status", "?")
        reason = res.get("reason", "")
        print("l2_cron: status=%s%s" % (status, (" reason=%s" % reason) if reason else ""))
        # exit non-zero only for an outright error; every gate verdict (skipped/abort/ceiling/dry_run/
        # ran) is a clean, expected outcome a scheduler should treat as success.
        return 1 if status == "error" else 0
    except Exception as e:  # belt-and-suspenders: never surface a traceback to the scheduler
        print("l2_cron: status=error reason=%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
