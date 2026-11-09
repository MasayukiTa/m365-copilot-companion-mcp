## 2026-11-09 - A wedged hard reset during Edge recovery no longer kills the fleet process

- Change: in `relay/fleet_runner.main()` the resume loop's pre-connect step
  "Edge unreachable -> hard reset before (re)connecting" ran its
  `_with_deadline(lambda: hard_reset(port), RECOVERY_STEP_DEADLINE_S, "hard_reset")`
  ABOVE the per-leg `try`. `_with_deadline` raises `FleetRecoveryTimeout` when the wrapped
  call does not return within its 60 s ceiling, and a genuinely wedged browser can make the
  hard reset itself blow that ceiling. Outside the `try`, nothing in the recover loop caught
  it: the exception reached `main()` and ended the process with `SystemExit` mid-recovery.
  Fix: move the unreachable+hard-reset step INSIDE the leg `try`, so a blown hard-reset
  deadline is caught by the generic `except Exception` handler that calls `_after_failed_leg`
  (count the failed leg, back off, retry) -- exactly what `FleetRecoveryTimeout`'s own
  docstring already promises ("counted as a failed attempt and RETRIED through the ordinary
  reconnect path"). Recovery now survives past the attempt that previously killed it.
- Evidence: coordinator_20261109_183127_p35696.log. 22:22:08 context lost (30 goals) ->
  22:25:20 watchdog hard reset ('close_idle_pages' blew its deadline by 95 s -> wedged) ->
  22:30-22:32 three holds inside the 300 s start-up grace -> 22:32:36 reset (browser does not
  answer DevTools port) -> 22:35:15 attempt 2/3 -> 22:37:17 'Edge unreachable -> hard reset'
  then `FleetRecoveryTimeout: recovery step 'hard_reset' did not return within 60s`. 30 goals
  were abandoned and attempt 3 was never reached -- the process was gone.
- Behaviour: unchanged on the healthy path. A hard reset that returns within its deadline
  behaves as before; only a hard reset that BLOWS the deadline now counts a leg and retries
  instead of terminating the run. `_recovery_step` (no give-up while goals remain) and the
  `--max-recover` back-off are untouched.
- Tests: `relay/test_a_wedged_hard_reset_in_recovery_is_caught_not_fatal.py` (hermetic, in
  CI and in the manifest) statically asserts, via AST over `relay/fleet_runner.py`, that every
  `_with_deadline(..., "hard_reset")` call in `main()` is lexically inside a `try`, and that at
  least one enclosing `try` catches `Exception` (so `FleetRecoveryTimeout`, an `Exception`
  subclass, is actually handled). A negative control confirmed the pre-fix placement fails the
  check. No Edge/Playwright/Windows calls, so it runs on the Linux CI.
- Open: this makes recovery SURVIVE a wedged hard reset; it does not by itself PREVENT the
  pre-wedge memory thrash (free RAM 778 MB / ~48 msedge at 21:42) that produced the wedge.
  A separate pre-wedge recycle in the watchdog (recycle the companion Edge while its DevTools
  port still answers, on the measured RECYCLE_* thresholds) is the complementary prevention
  and is tracked on its own branch.
