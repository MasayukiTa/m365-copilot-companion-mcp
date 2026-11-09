## 2026-11-07 - `/health` sub-probes run in a dedicated pool under a deadline (slow liveness misread as a dead server)

- Problem. The `/health` liveness route ran every heavy sub-probe (`_server_identity`,
  `_auth_stats_summary`, `_self_test_summary`, `_tool_probe_summary`, `_tunnel_channels_summary`,
  `fleet_tool_health.get_summary`) via `asyncio.to_thread`, which dispatches onto the event loop's
  DEFAULT `ThreadPoolExecutor` -- the SAME pool that runs tool bodies (`shell_exec`, `find_files`,
  `run_python`, `pwsh_exec`). When a long tool call holds every pool slot (measured 2026-11-04: a
  single `pwsh_exec` held a slot 150s+ while others piled up behind it), `/health`'s own
  `to_thread` calls cannot get a worker and the probe times out. The fleet send-gate
  (`_mcp_gate_blocked`) reads `/health` to decide readiness, so a slow `/health` is misread as a
  dead server and every worker falls into `token_wait` (surfacing as `All connection attempts
  failed`) even though the server, Edge CDP :9222 and port 8000 are all up.

- Reproduced live 2026-11-07. Under CPU=96.8% a single `GET /health` took 20.35s (hit the 20s
  client timeout); a 5x probe loop blew past 120s. `call_tool` itself intermittently returned
  `Client failed to connect: All connection attempts failed` -- the same starvation hitting the
  tool gateway. As the long tool calls finished on their own (non-destructive; nothing killed),
  CPU fell to 55.8% and `/health` returned in 2.23s, confirming the cause was pool starvation,
  not a dead server.

- Change. Give `/health` its own small, bounded `_HEALTH_POOL` (`ThreadPoolExecutor`,
  `max_workers=4`), separate from the default/tool executor, so a saturated tool pool can never
  starve the liveness probe. `_gather_health_probes()` runs all heavy sub-probes concurrently in
  that pool under ONE shared deadline `_HEALTH_PROBE_BUDGET_S` (default 2.0s, env-overridable via
  `MCP_HEALTH_PROBE_BUDGET_S`). Past the budget the route answers with the minimal liveness fields
  plus whatever probes finished -- a clear, bounded answer instead of a hung connection; a probe
  that is slow, raises, or returns a non-dict simply contributes nothing and never 500s or hangs
  the route. Stragglers are left to finish on their own thread. Adds `server_uptime_s` (pure
  arithmetic) so a caller can tell answered-but-slow (probes dropped) from answered-instantly.

- Non-destructive. No process, tool, or browser was killed; the long tool calls were left to
  finish on their own. The fix removes the starvation path rather than clearing the load by force.

- Tests. `scripts/test_health_probe_isolation.py` (registered in `.github/workflows/ci.yml`),
  8 passing locally: fast probes merge; a slow probe is dropped within budget; a raising/non-dict
  probe is ignored; the pool is separate and bounded; the budget stays a positive float;
  `health_pool_survives_a_saturated_default_executor_under_cpu_load` reproduces the root cause with
  real CPU-bound load on the default pool and asserts `/health` still answers within budget; and
  `health_handler_actually_merges_probe_fields` pins end-to-end that the handler awaits the gather
  helper and merges probe fields into the reply.

- PR. #461 (branch `fix/gwslow-health-pool`, off `origin/main` @ 9ff412b0).

### 2026-11-07 (later, UTC) - post-deploy verification on the production PC

- Confirmed in production. Before the deployed server picked up #461, the live process
  (`server_head=dd5b5acb`, which does NOT contain #461) served `GET /health` in 15.7-41.1s under
  CPU=100%, while the c2c-bridge `/health` answered in 3-6ms at the same instant -- isolating the
  slowness to the MCP server's probe path, not the network or CPU per se. After the supervisor
  cycled the server onto the merged code (`server_uptime_s=106`, i.e. the restart was the
  supervisor's own, not a manual kill), live `GET /health` returned in 2.20s / 2.02s / 2.06s --
  pinned at the 2.0s `_HEALTH_PROBE_BUDGET_S` deadline, i.e. the dedicated-pool + shared-budget
  path is live and working.
- Tests re-run green on `origin/main` @ 442e37d1 (includes #461):
  `scripts/test_health_probe_isolation.py` 8 passed.
- Non-destructive: no process/tool/browser was killed; the production checkout and its running
  process were left untouched. Verification ran in a throwaway worktree off `origin/main`.

