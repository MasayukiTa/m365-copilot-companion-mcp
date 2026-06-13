# SWE-bench harness failure-mode registry

Living catalogue of failure *classes* found while training the scaffold toward the
final 300-instance run. Goal: each accident happens **once** — observe → root cause →
**domain-general** fix → prove it doesn't recur → move on. No instance-specific hacks
(that's overfitting); only fixes that generalize. Staged batches (12 → 30 → 60 → …)
are the training ground; their scores are burned, the holdout measures real skill.

Status: ✅ fixed & verified · 🟡 mitigated/operational · ⛔ open

| # | Failure class | Root cause | Fix | Status |
|---|---|---|---|---|
| 1 | MCP server (main.py) event-loop freeze: port LISTENING but every request times out, CLOSE_WAIT pile-up, FD exhaustion → supervisor restart churn → Copilot loses all tools, all workers STUCK | FastMCP runs each tool inline on the single uvicorn event loop; all 138 tools were sync `def`, so one heavy tool (run_python 60s, etc.) froze the whole loop | Wrap every sync tool in an async/threadpool wrapper (`registry._to_async`); jobs FD-leak fix; faulthandler; `/health` route; uvicorn graceful-shutdown 30s | ✅ 9c2763b |
| 2 | supervisor can't detect the freeze / can't restart | TCP-only liveness check; a wedged main.py keeps the port bound so the new instance can't bind | `/health` HTTP probe; kill stale port owner before (re)start; debounce 2→4, interval 10→15; default TunnelName fix | ✅ 9c2763b, 204506e, e242f17 |
| 3 | send no-ops / "composer still holds text": message never submitted | `locator.count()` does not auto-wait → raced the late-arming Send button; dead-tab sends retried | re-resolving arm poll; `ConversationClosed` terminal; multi-candidate send selector | ✅ 5148c02 |
| 4 | send clicks an imposter button | broad `aria-label*="送信"` matched the composer-expand toggle and `フィードバックを送信` | candidate priority (exact first) + imposter blacklist (expand/collapse/dictation/voice/feedback) | ✅ 2ecf873, abc452d |
| 5 | send STUCK on slow turns: `Locator.click: Timeout 30000ms` ×10 even with a correct patch | sent while previous turn still generating (`is_processing=True`); unbounded `composer.click()` hung 30s; counted as transient failure | GenerationInProgress gate (waits ≤240s, separate `gen_waits` counter, never touches transient budget); `set_default_timeout(8000)`; `composer.click(timeout=5000)` | ✅ abc452d |
| 6 | verify gate returns a stale FAIL: agent fixes the bug but is told it still fails forever | `run_id` constant per instance; swebench skips an already-run instance ("1 instances already run, skipping") and re-reads the prior report → the new patch is never evaluated | purge `logs/run_evaluation/<run_id>`, `companion.<run_id>.json`, `<run_id>.*.json` before each run | ✅ aa77bcb |
| 7 | RESOLVED never detected though the fleet resolved it | final snapshot lacked `cwd`; orchestrator matched instances by cwd | carry cwd in final snapshot; also recover instance id from the goal's `wt_<instance>` path | ✅ cefc2a6 |
| 8 | goal never reaches the agent → 10 empty retries | generic RETRY sent instead of the goal when the agent says "no task presented" | `goal_not_seen()` detector → re-send the goal verbatim (max 3) | ✅ (batch-12 tooling commit) |
| 9 | C: disk fills: eval images (2.8–7.7 GB each) inflate the WSL vhdx which lives on C: | swebench keeps per-instance images; no cleanup | `docker rm -f` container + `rmi` image + prune after each verdict (`SWE_KEEP_IMAGES=1` to skip) | ✅ 0583454 |
| 10 | C: starves under concurrency=2: pagefile balloons to ~19.6 GB on a 16 GB box (heavy sklearn/matplotlib builds + 2 Copilot tabs) | memory pressure → Windows grows pagefile.sys on C: | run at `--max-concurrent 1` (one tab + one eval); auto-cleanup keeps vhdx flat | 🟡 operational (concurrency=1) |
| 11 | vhdx physical never shrinks after image deletion | `set-sparse` (non-admin) only prevents future growth; `diskpart compact` reclaims only zeroed blocks (0 GB on a 1 TB-provisioned vhdx); zero-fill is unsafe | none viable headless; reboot resets the bigger consumer (pagefile). `compact_miasma_vhdx.ps1` exists for an elevated manual run | 🟡 operational |
| 12 | per-tab tool-absence race: a freshly opened Copilot tab occasionally has no local-file MCP tools, agent STUCKs "tools absent" and burns retries (distinct from #1 — main.py is healthy) | MCP tool list not loaded by the time the conversation starts | **OPEN**: detect "tools absent" STUCK → abandon tab, reopen a fresh conversation for the goal instead of retrying | ⛔ open |
| 13 | generation-wait froze the round-robin: status.json went stale ("フリート停止?"), and at concurrency>1 it would starve other workers | the send-hardening (#5) made the fleet path wait the full 240s for a slow turn inside the single-thread sweep | fleet `_begin_send` passes `gen_wait_s=2.0` → short non-blocking check then defer via GenerationInProgress; patience realized across `max_gen_waits` (60) deferrals | ✅ 7a355b5 |
| 14 | watchdog hard-reset the Edge mid-eval → every in-flight goal restarted at attempt 1 (sphinx-8595 t7→t1, 7 turns lost; slow/eval-heavy instances could loop forever) | `_salvage_via_checks`→`run_all_blocking` runs the docker eval (~1300s) synchronously, freezing status.json; the stall watchdog read that as a wedged Edge | workers publish `eval_busy_until`; `_watchdog_should_reset` skips reset while a worker is verifying within its eval deadline; `EVAL_STALL_CEILING_S=1500` fail-safe still recovers a truly wedged mid-verify Edge | ✅ a5c69e6 |
| 15 | cockpit header subtitle vanished under the RAM controls | header was a non-clipping DockPanel; the unbounded subtitle ran under the opaque right-docked controls | 2-column Grid + `TextTrimming.CharacterEllipsis` (truncate at the edge, full text on hover) | ✅ faa22fc |
| 16 | failure feedback was BLIND for django/sympy (~60% of Lite): agent retried with no failing-test name or assertion — the single biggest suppressor of the retry loop | `_failure_feedback` only parsed pytest output; django (unittest) gave just `FAILED (errors=1)`, sympy (custom runner) gave nothing | `_parse_failure_log` extracts test names + errors + raised-at across pytest / django / sympy formats; capped, raw-tail fallback, fully guarded. Verified on 32 real logs | ✅ 28caa9a |
| 17 | leaked eval container on EVAL_TIMEOUT: a detached `docker run` keeps running after the wsl.exe subprocess is killed (seen: sympy-11870 "Up 33 min" after its turn ended), holding RAM + inflating the C: vhdx | the timeout path returned without calling `_cleanup_docker` (success/fail paths did) | call `_cleanup_docker` (force `docker rm -f` + `rmi`) on timeout too | ✅ 1317f41 |
| 18 | acceptance gate (the durable, non-SWE asset) gave only a raw output tail on failure | the structured multi-format failure extractor was swe_check-only | shared `relay/test_feedback.py::summarize_test_failure` (pytest/django/sympy) wired into `acceptance.Check` for pytest/python checks | ✅ ea4b123 |
| 19 | eval FALSE-NEGATIVE on sphinx-8595: a correct (gold-superset) patch graded `resolved:false` though the test passes in-container (`1 passed, exit 0`) | swebench reset: a new-file-only test_patch → `get_modified_files()==[]` → reset degenerates to a **bare `git checkout base`** that reverts the setup commit injecting `-rA` into tox.ini → pytest dots → log-parser misses the PASSED line. Blast radius: 1/300 (only sphinx-8595 combines new-file-only test_patch + a parser-relevant pre_install) | **OPEN (must fix before the holdout)**: inject `-rA` via `PYTEST_ADDOPTS` (env survives the checkout) instead of the tracked tox.ini sed; apply when the fleet is idle, not on the live critical path | ⛔ open |

## Gaps found by the adversarial completeness audit (queued for an idle-window pass)

These are real but non-blocking; fix as one coordinated pass at a round boundary (live-path edits take effect on the next verify/round, never mid-eval).

| # | Gap | Why it matters | Planned fix | Priority |
|---|---|---|---|---|
| 20 | timeout nesting leak: swe_check worst-case wall = wsl(1200) + cat(60) + cleanup(120) ≈ 1380s **>** the acceptance `Check.timeout=1300`, so the acceptance layer kills swe_check (python) before its own timeout+cleanup → the detached eval container leaks (re-opens #17 one layer up, on the heaviest sklearn/matplotlib/sympy pulls) | container/RAM/disk leak on the exact heavy instances that already strain C: | raise acceptance Check timeout to ≥1500 in `swe_batch_setup.py` (next round) so swe_check finishes + cleans up under it | medium |
| 21 | gen-wait patience is **deferral-count**-bounded (60×~4s≈240s), not wall-clock; under CPU/sweep contention realized patience drifts from 240s → a legitimately-slow tail turn can false-STUCK (the #5 class at the tail) | discards a correct-but-slow django/sympy turn | bump `max_gen_waits`→~90 or make the bound wall-clock (`first_defer_ts+360s`) in relay | low-med |
| 22 | failure parser misses **doctest** (`Failed example:`/`Expected:`/`Got:` — sympy/sphinx) and **import/collection errors** (`AppRegistryNotReady` with no `FAIL: test_x` header — django) → agent gets a file:line or error but **no test name / no expected-vs-got** | blind-ish retries on sympy/sphinx doctests + django import errors (partial regression of #16 for those subcases) | add doctest + import-error branches to `relay/test_feedback.py` (and dedup swe_check's copy into it) | low-med |

Also confirmed acceptable/monitored (do NOT block the run): same-instance purge race (safe at concurrency=1), `eval_busy_until` leak after a crash-during-verify (bounded by the 1500s failsafe), `summarize_test_failure` not on the SWE path by design (swe_check uses its own copy — drift risk only), silent purge failure (add a stderr breadcrumb opportunistically).

## Observability that paid off
- `.fleet/transcripts/<run_id>_<name>.jsonl` — full per-turn conversations (untruncated), keyed per run so reused worker names never interleave.
- `.fleet/send_failures.jsonl` — one snapshot per failed send (button match/label/visible, composer len, tab visibility, is_processing). Pinned #3, #4, #5.
- `.fleet/delete_log.jsonl` — per-attempt conversation-delete results.
- `bench/swe_heartbeat.py` — one-line periodic pulse (resolved/round/C:/workers).

## How fixes land without stopping a running batch
- `bench/swe_check.py` is spawned fresh per verify → edits take effect on the **next verify**.
- `relay/*.py` is imported by the running `fleet_runner` → edits take effect on the **next chunk launch**. Always keep the file `py_compile`-clean so a mid-run reload never grabs broken code.
