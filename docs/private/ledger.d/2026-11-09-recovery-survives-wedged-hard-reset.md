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

- Two distinct failures, not one -- keep them separate:
  (A) WEDGE DETECTION trigger (what the watchdog saw): a RECOVERY PHASE blew its own deadline.
      Loss 2 line 3258: "recovery phase 'close_idle_pages' blew its deadline by 95s -> wedged".
      This is NOT the hard_reset deadline; it is the close_idle_pages phase of recovery.
  (B) PROCESS-KILLING exception (what ended the run): on the NEXT attempt the pre-connect
      `_with_deadline(hard_reset, 60s)` itself blew its 60 s ceiling and, being outside the
      try, raised `FleetRecoveryTimeout` up to `main()` -> `SystemExit` (lines 3286-3289).
  The fix in this PR addresses (B) -- survival. (A), and the memory thrash that produced the
  wedge, are prevention and are tracked separately (see Open).

- Loss 1 - 21:19:56 "Edge context lost; resuming 43 goal(s) (attempt 1/3)" (line 2928):
  - What the watchdog saw: context lost on a healthy coordinator; resume ran cleanly and the
    fleet rehydrated 202 unmerged campaigns at 21:22:28. No wedge, no hard reset, no process
    death on this loss.
  - Sweep laps in the 5 min before (lines 2853-2922): 21:12:50 lap 1358 6.37s; 21:13:39 lap
    1360 20.98s; 21:14:29 lap 1362 30.10s; 21:16:00 lap 1365 9.70s; 21:16:38 lap 1369 13.88s;
    21:17:17 lap 1372 12.12s; 21:17:48 lap 1375 13.75s; 21:18:29 lap 1377 16.15s; 21:19:03
    lap 1381 7.55s; 21:19:37 lap 1386 5.65s. Mostly normal (6-17s) with two spikes (21/30s).
  - DevTools: the port still answered throughout (the 21:38-21:51 watchdog lines, after this
    loss, all read "browser still answers its DevTools port (within 450s ceiling)").
  - free RAM / msedge count / total working set: NOT emitted to this log at per-sweep cadence.
    The coordinator logs free RAM only at start-up ("free RAM now 2098 MB", line 30) and on
    autoscale ticks, not a rolling RAM/msedge/working-set series. The 21:42 snapshot in the
    task (CPU 89%, free RAM 778 MB, 48 msedge) comes from the machine-state capture, not from
    coordinator_*.log; the log cannot corroborate it line-for-line.
  - attempt chain: attempt 1/3 only; it succeeded (resume completed), so attempts 2/3 and 3/3
    were never needed.

- Loss 2 - 22:22:08 "Edge context lost; resuming 30 goal(s) (attempt 1/3)" (line 3253):
  - Sweep laps in the 5 min before (lines 3181-3251): 22:14:33 lap 70 91.93s; 22:15:33 lap 71
    59.99s; 22:16:28 lap 72 54.74s; 22:17:15 lap 73 47.38s; 22:18:54 lap 74 98.55s; 22:20:07
    lap 75 73.50s; 22:20:44 lap 76 36.74s; 22:21:20 lap 77 36.01s; 22:22:07 lap 79 26.74s.
    Every lap 27-99s -- far above the 6-17s normal band; the box was already thrashing.
  - attempt / wedge / kill chain (exact log lines):
      22:22:08 attempt 1/3 begins (line 3253).
      22:25:20 watchdog: "fleet stalled 150s -> hard-resetting the Edge (recovery phase
               'close_idle_pages' blew its deadline by 95s -> wedged)" -- WEDGE TRIGGER (A),
               line 3258.
      22:26:53 post-reset sweep lap 1 took 123.73s (line 3261); BrokenPipeError on the dead
               Edge socket (lines 3262-3269).
      22:30:12 / 22:31:13 / 22:32:17 watchdog holds: "status frozen ... but not resetting:
               Edge (re)started ...s ago (within 300s start-up grace)" (lines 3270-3272) --
               three holds inside the 300 s start-up grace, so no reset yet.
      22:32:36 watchdog: "fleet stalled 150s -> hard-resetting the Edge (stalled 296s and the
               browser does not answer its DevTools port -> wedged)" (line 3274) -- DevTools
               now unresponsive (contrast loss 1, where it always answered).
      22:35:15 attempt 2/3 begins (line 3276).
      22:37:17 "Edge unreachable -> hard reset before (re)connecting" (line 3277), then the
               pre-connect `_with_deadline(hard_reset, 60s)` blew its 60 s ceiling and raised
               `FleetRecoveryTimeout: recovery step 'hard_reset' did not return within 60s`
               from OUTSIDE the try -> `SystemExit` (lines 3278-3289) -- KILL (B).
  - "attempt 3/3 and after": there was NO attempt 3/3. The process died DURING attempt 2/3;
    attempt 3/3 was never reached and the 30 resuming goals were abandoned when the process
    exited. (The task's "attempt 3/3 failed, then what" framing does not match the log: the
    run never got a third attempt because the second one killed the process.) The very next
    coordinator process is a fresh start at 22:38:54 (coordinator_20261109_223854_p36568.log,
    "free RAM now 2506 MB"), i.e. a cold restart, not a 3rd in-process attempt.
  - DevTools response: discrete events only, not a measured latency series. Answering at
    22:30-22:32 (within start-up grace, implicitly reachable) then explicitly "does not answer
    its DevTools port" at 22:32:36. The log does not record a numeric DevTools round-trip time.
  - free RAM / msedge count / total working set at per-sweep cadence: again NOT in this log;
    the only in-log RAM figures are start-up/autoscale snapshots. Corroborating evidence for
    the thrash is the sweep-lap inflation above (27-99s) and the close_idle_pages phase itself
    blowing its deadline -- both consistent with the 21:42 machine snapshot, but the per-
    process working-set total is not available from coordinator_*.log.

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
  pre-wedge memory thrash (the sweep-lap inflation to 27-99s before loss 2, and the 21:42
  machine snapshot of CPU 89% / free RAM 778 MB / ~48 msedge) that produced the wedge, nor the
  close_idle_pages phase (A) blowing its own deadline. A separate pre-wedge recycle in the
  watchdog (recycle the companion Edge while its DevTools port still answers, on measured
  RECYCLE_* thresholds) is the complementary prevention and is tracked on its own branch.
