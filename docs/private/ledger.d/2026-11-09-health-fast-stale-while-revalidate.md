## 2026-11-09 - `/health` stays fast under CPU load: watched-tree walk leaves the probe path (stale-while-revalidate)

- Problem. Even after `/health` got its own bounded pool and a shared probe budget
  (see 2026-11-07-health-probe-dedicated-pool), one sub-probe still did real work ON the
  `/health` path: `_watched_code_changed()` called `tools.deploy_freshness.newer_than()`, a
  synchronous walk of the watched tree (hundreds of `stat()`s). Measured live 2026-11-07
  09:29-09:31: under total CPU 99.5% five consecutive `GET /health` calls took 29.6s, 25.3s,
  15.7s, 8.7s and 19.0s, while the c2c-bridge answered `/health` in 278ms at the same moment --
  so the slowness was INSIDE the server, not the network. Because the walk could not finish
  inside the health-pool budget, every poll came back partial and the fleet send-gate read a
  starved-but-alive server as 'down'; workers w19/w21/w22 fell to `All connection attempts
  failed` and the supervisor restarted the server four times between 08:32 and 08:36.

- Change. `_watched_code_changed()` is now stale-while-revalidate. It serves the last known
  cached answer instantly and, when that answer is older than the cache TTL, kicks exactly ONE
  background daemon thread (`_watched_recompute`) to run `newer_than()` off the hot path;
  concurrent pollers that find the cache stale get the OLD value immediately and never start a
  second walk (guarded by `_watched_refresh_lock` + `_watched_refreshing`). The only time the
  walk runs synchronously is the very first, pre-traffic call at warm-up. `newer_than()` never
  runs on a `/health` poll under load again. `_gather_health_probes()` already harvests partial
  results at the shared budget and reports `health_degraded=True` + `health_probes_pending=<n>`,
  so a starved server now returns a clear BUSY answer instead of a dropped connection.

- Behaviour. Once warm, a `/health` poll does no tree walk on the caller's thread; a burst of
  callers against a stale cache triggers at most one background walk, not one per caller. When
  sub-probes miss the budget the route still returns `status: ok` plus the fast fields and sets
  `health_degraded` with the pending count, rather than hanging. The guarantees are thread- and
  flag-identity facts, independent of how slow the box or the walk is -- no wall-clock assert.

- Non-destructive. No process, tool, or browser was killed; the fix removes the walk from the
  probe path rather than clearing load by force.

- Tests. `tests/test_health_fast_under_cpu_load.py` (3, green locally and in CI), registered in
  `.github/workflows/ci.yml` and the hermetic manifest: (1) `_gather_health_probes` honours a
  0.3s budget under real CPU load, keeps the fast probe's result, and reports
  `health_degraded`/`health_probes_pending=2` without waiting on two 10s blockers; (2) once
  warm, the watched-tree walk runs on a background thread, never the caller's, with <=1 walk per
  burst of 50 callers; (3) the real `main.health` handler returns a JSONResponse with status ok
  across five consecutive polls and surfaces `health_degraded` under a 0.3s budget + CPU load.
  Existing `tests/test_health_route.py` (10) still green.

- Open. None. Follows on from PR #461 (`fix/gwslow-health-pool`); this slice is PR #506
  (branch `fix/gwslow-health-fast`).
