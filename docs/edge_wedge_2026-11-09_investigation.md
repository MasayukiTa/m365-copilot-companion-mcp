# Edge wedge investigation -- 2026-11-09

Read-only reconstruction of the two fleet-Edge losses on 2026-11-09 from
`.fleet/coordinator_*.log` and the watchdog decision code in
`relay/fleet_runner.py` (`_watchdog_should_reset`, recovery loop). No log or
worker state was modified.

## Watchdog thresholds (relay/fleet_runner.py)

| Constant | Value | Role |
|---|---|---|
| `--stall-s` | 150s | status.json frozen this long -> start adjudicating a reset |
| `CDP_RESPONSIVE_STALL_CEILING_S` | 450s | browser still answers DevTools port -> wait, up to this |
| `EDGE_START_GRACE_S` | 300s | Edge (re)started this recently -> do not reset yet |
| `CAPTURE_STALL_CEILING_S` | 300s | a token capture in flight may explain the freeze, up to this |
| `EVAL_STALL_CEILING_S` | (verify ceiling) | a declared verify/eval may hold the main thread, up to this |
| `RECOVERY_STEP_DEADLINE_S` | per-step | a recovery step past its own deadline -> the recovery itself is a wedge |

## Loss 1 -- 21:19:56  (log p35696, line 2928)

**What the watchdog saw:**
- `[fleet 98/132]` with most workers `[停滞]` (stalled); machine at 21:42 was
  CPU 89%, free RAM 778MB, 48 msedge processes; sweep laps 47-166s vs normal 6-17s.
- status.json frozen, but the browser kept **answering its DevTools port**, so the
  CDP-responsive branch held the reset off repeatedly within the 450s ceiling:
  - 21:38:42 frozen 150s -- not resetting (answers DevTools, within 450s)
  - 21:48:10 frozen 154s -- not resetting (same)
  - 21:51:33 frozen 154s -- not resetting (same)

**What the watchdog did:** it did **not** hard-reset here. The actual context
loss at 21:19:56 was recorded as `[recover] Edge context lost; resuming 43
goal(s) (attempt 1/3)` -- Edge dropped the context on its own under load; the
watchdog then resumed the 43 in-flight goals by id.

**Decision path:** `cdp_responsive=True` + `stalled_s <= 450` ->
`"browser still answers its DevTools port (within 450s ceiling)"` = do not reset.
The loss was not a watchdog reset; it was the browser collapsing under the
memory/CPU pressure the watchdog had no admission control to prevent.

## Loss 2 -- 22:30-22:37  (log p35696, lines 3253-3277)

**What the watchdog saw and did, in order:**
1. 22:22:08 `[recover] Edge context lost; resuming 30 goal(s) (attempt 1/3)`
2. 22:25:20 `[watchdog] fleet stalled 150s -> hard-resetting the Edge (recovery
   phase 'close_idle_pages' blew its deadline by 95s -> wedged)` -- the
   **recovery step itself** exceeded `RECOVERY_STEP_DEADLINE_S`, which the code
   treats as a wedge even while the port answers.
3. 22:30:12 / 22:31:13 / 22:32:17 frozen 152/213/278s -- **not** resetting:
   `Edge (re)started ...s ago (within 300s start-up grace)` (`EDGE_START_GRACE_S`).
4. 22:32:36 `[watchdog] fleet stalled 150s -> hard-resetting the Edge (stalled
   296s and the browser does not answer its DevTools port -> wedged)` -- now
   `cdp_responsive=False`, so the final branch fires.
5. 22:35:15 `[recover] ... resuming 30 goal(s) (attempt 2/3)`
6. 22:37:17 `[recover] Edge unreachable -> hard reset before (re)connecting`

**Decision path:** start-up grace (300s) held resets off while Edge was young,
then `cdp_responsive=False` + nothing explaining the silence ->
`"stalled ...s and the browser does not answer its DevTools port -> wedged"` =
hard reset. 30 goals were resumed across attempts 1 and 2.

## Common cause

Both losses trace to the same thing: tabs kept being admitted until the machine
hit CPU 89% / free RAM 778MB / 48 msedge processes, at which point Edge either
dropped its context (loss 1) or stopped answering its DevTools port (loss 2).
Every watchdog "do not reset" branch is bounded by a ceiling, so the watchdog
behaved correctly -- it waited while the port answered, respected the start-up
grace, and reset once the browser went truly unreachable. The watchdog is a
*recovery* mechanism; it had no *admission* lever to stop the fleet climbing into
the wedge in the first place.

## Fix (PR #522, branch fix/edge-wedge-preemptive-cap)

`_preemptive_tab_ceiling` stops admitting new tabs **before** the observed
failure point: RAM floor 1024MB (above the 778MB seen at failure) and process
cap 40 (below the 48 seen), clamping the autoscale target to the already-open
tab count without closing any in-flight tab. Wired into `relay_fleet` admission;
12-case pytest suite registered in `ci.yml`.
