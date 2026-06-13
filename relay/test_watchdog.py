"""Tests for the fleet watchdog's wedged-vs-busy decision (fleet_runner._watchdog_should_reset).

The watchdog hard-resets the dedicated Edge when status.json stops advancing -- the recovery
for a genuinely WEDGED Edge (CDP dead, main thread blocked in a sync attach). But a frozen
status.json ALSO occurs when the main thread is legitimately blocked in a BOUNDED acceptance
eval (the SWE-bench docker verify takes minutes, > --stall-s). Resetting then throws away the
eval and resumes every goal at attempt 1 (the observed sphinx-8595 t7->t1 regression).

These tests prove the decision distinguishes the two:
  * verify in flight (eval_busy_until in the future)  -> DON'T reset (within the eval deadline)
  * verify status but no deadline, within ceiling      -> DON'T reset (bounded by the ceiling)
  * verify but frozen past the eval ceiling            -> reset (failsafe: a real wedge mid-verify)
  * no verify in flight, frozen past stall_s           -> reset (genuinely wedged)
  * not running / idle / no stall                      -> never reset

Run:  .venv\\Scripts\\python.exe relay\\test_watchdog.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.fleet_runner import _watchdog_should_reset
from relay.relay_fleet import EVAL_STALL_CEILING_S

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def _status(workers, running=True, idle=False):
    return {"running": running, "idle": idle, "updated": 1.0, "workers": workers}


def main():
    now = 1000.0
    STALL = 150            # the default --stall-s

    # 1. genuinely wedged: a worker is "waiting" (generating), no eval marker, frozen > stall_s
    #    -> reset (this is the ONLY path that should fire -- a true CDP death).
    st = _status([{"name": "w0", "status": "waiting", "eval_busy_until": 0.0}])
    should, why = _watchdog_should_reset(st, stalled_s=STALL + 1, now=now)
    check("wedged_no_eval_resets", should is True and "wedged" in why)

    # 2. blocking eval in flight (eval_busy_until in the future): the main thread is legitimately
    #    busy in a bounded eval -> DON'T reset even though status.json is frozen way past stall_s.
    st = _status([{"name": "w0", "status": "verifying", "eval_busy_until": now + 600}])
    should, why = _watchdog_should_reset(st, stalled_s=STALL + 300, now=now)
    check("eval_busy_future_no_reset", should is False and "within eval deadline" in why)

    # 3. one of several workers is mid-eval -> still don't reset the WHOLE Edge (it would
    #    discard that worker's eval and resume every goal at attempt 1).
    st = _status([
        {"name": "w0", "status": "waiting", "eval_busy_until": 0.0},
        {"name": "w1", "status": "verifying", "eval_busy_until": now + 900},
    ])
    should, _ = _watchdog_should_reset(st, stalled_s=STALL + 500, now=now)
    check("any_worker_mid_eval_no_reset", should is False)

    # 4. verify STATUS but no future deadline (old snapshot w/o eval_busy_until), within the
    #    global ceiling -> don't reset (bounded by the ceiling from the freeze duration).
    st = _status([{"name": "w0", "status": "verifying"}])   # no eval_busy_until key at all
    should, why = _watchdog_should_reset(st, stalled_s=EVAL_STALL_CEILING_S - 10, now=now)
    check("verify_status_within_ceiling_no_reset", should is False and "ceiling" in why)

    # 5. FAILSAFE: a verify status frozen PAST the eval ceiling is treated as a real wedge that
    #    merely happened to be mid-verify -> reset, so recovery is never permanently disabled.
    st = _status([{"name": "w0", "status": "verifying"}])
    should, why = _watchdog_should_reset(st, stalled_s=EVAL_STALL_CEILING_S + 60, now=now)
    check("verify_past_ceiling_resets", should is True and "wedged" in why)

    # 6. a STALE eval deadline (in the PAST) is NOT a free pass: it falls back to the ceiling.
    #    Within the ceiling -> wait; past it -> reset.
    st = _status([{"name": "w0", "status": "verifying", "eval_busy_until": now - 50}])
    should, _ = _watchdog_should_reset(st, stalled_s=EVAL_STALL_CEILING_S - 10, now=now)
    check("stale_deadline_within_ceiling_waits", should is False)
    st = _status([{"name": "w0", "status": "verifying", "eval_busy_until": now - 50}])
    should, _ = _watchdog_should_reset(st, stalled_s=EVAL_STALL_CEILING_S + 60, now=now)
    check("stale_deadline_past_ceiling_resets", should is True)

    # 7. not running / idle / no stall yet -> never reset (caller resets its stall clock).
    check("not_running_no_reset",
          _watchdog_should_reset(_status([], running=False), STALL + 1, now)[0] is False)
    check("idle_no_reset",
          _watchdog_should_reset(_status([], idle=True), STALL + 1, now)[0] is False)
    check("no_stall_no_reset",
          _watchdog_should_reset(_status([{"name": "w0", "status": "waiting"}]),
                                 stalled_s=0, now=now)[0] is False)

    # 8. malformed eval_busy_until must not crash the decision (watchdog runs unattended).
    st = _status([{"name": "w0", "status": "waiting", "eval_busy_until": "garbage"}])
    should, _ = _watchdog_should_reset(st, stalled_s=STALL + 1, now=now)
    check("malformed_busy_until_safe", should is True)

    # 9. empty / None status never resets (a missing snapshot is not evidence of a wedge).
    check("none_status_no_reset", _watchdog_should_reset(None, STALL + 1, now)[0] is False)
    check("empty_status_no_reset", _watchdog_should_reset({}, STALL + 1, now)[0] is False)

    print("\n=== %d/%d watchdog checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
