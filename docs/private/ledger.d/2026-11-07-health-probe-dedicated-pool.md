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

### 2026-11-07 (later, UTC) - production verification, corrected

- How the fix is actually live. The listening server on 127.0.0.1:8000 is pid 26916
  (`python main.py`, cwd `C:/Users/M118A8586/resonac-mcp`). That checkout is on branch
  `feat/judge-gate-confirm-overlap-order` at HEAD `dd5b5acb`, and `git status` shows `main.py` as an
  UNCOMMITTED working-tree modification (`M main.py`). That modified `main.py` already contains
  `_HEALTH_POOL`, `_gather_health_probes`, `_HEALTH_PROBE_BUDGET_S`, `server_uptime_s` (plus newer
  `resp_total_10m`/`resp_inflight` fields). So the dedicated-pool + deadline fix is live in
  production via an un-committed working-tree edit of `main.py`, NOT via the HEAD being on the
  merged code: `dd5b5acb` does NOT contain the #461 merge commit `eeaa1994`
  (`git merge-base --is-ancestor eeaa1994 dd5b5acb` -> NO).
- Measured now on the live server: `GET /health` 2038 / 2089 / 2084 / 2049 / 2104 ms, pinned at the
  2.0s budget -- confirming the dedicated-pool + shared-budget path is what is running. c2c-bridge
  stayed in the low-ms range. Earlier "2.20/2.02/2.06s on merged code" phrasing was imprecise: the
  running code is the working-tree edit, and `origin/main` (which DOES contain #461 `eeaa1994`) is
  the committed home of the same fix.
- Tests: `scripts/test_health_probe_isolation.py` 8 passed on `origin/main` (contains #461).
- Non-destructive: no process/tool/browser was killed; the production checkout and its running
  process were left untouched.

### The pre-existing `health-threadpool-isolate` worktree -- merged? why it did not prevent this

- There is a pre-existing worktree `C:/Users/M118A8586/resonac-mcp/.claude/worktrees/health-threadpool-isolate`
  on branch `fix/health-liveness-own-executor`, HEAD `27a511af` (same title: "run /health sub-probes
  in a dedicated pool under a deadline").
- NOT merged. `git merge-base --is-ancestor 27a511af origin/main` -> NO. Its tree `b36f16b9` differs
  from the merged #461 tree (`eeaa1994` -> `a46963f3`). #461 was landed from a separate branch
  `fix/gwslow-health-pool` (head `11ed8ec9`, squash/rebase-merged as `eeaa1994`), so `27a511af`
  itself never reached `origin/main`.
- Why it did not prevent the outage. Because `fix/health-liveness-own-executor` (27a511af) stayed an
  un-merged side worktree, the running production server kept serving the OLD `/health` that
  dispatched heavy sub-probes onto the shared default ThreadPoolExecutor. The isolation/deadline fix
  existed only in that worktree, so under tool-pool saturation `/health` still starved (15-41s) and
  the fleet send-gate misread it as a dead server. The outage was only prevented once equivalent
  code was actually applied to the running `main.py` (now live as the uncommitted working-tree edit
  above) and landed on `origin/main` via #461.
