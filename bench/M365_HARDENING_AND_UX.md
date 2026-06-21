# M365 companion — hardening + UI/UX roadmap

Date: 2026-06-21

## Strategy (decided)

Specialize on the M365-Copilot substrate (its structural moat = enterprise Graph/SharePoint/Teams
context + zero per-token cost via the M365 seat). Do NOT chase native-API raw-quality/robustness
parity — the UI-driving substrate has a structural ceiling there. Instead:

1. **Harden** the substrate to production-grade so its *fragility* stops being the story.
2. **Close the achievable UI/UX gap** with Claude Code.
3. **Amplify where we can surpass** — live N-worker fleet, the self-improvement controller, enterprise
   context — things a single-session CLI structurally cannot show.

Goal: reach a level where "Claude Code, or the M365 agent?" is a genuine side-by-side question.

## Timing constraint

`relay_fleet.py`, `fleet_runner.py`, `edge_recover.py`, `swe_repos_setup_batch.py` are re-spawned
per chunk by the running large-N measurement. **Editing them now would make the A/B's ON and OFF arms
run different code.** So: design + measurement-safe NEW modules now; edit fleet internals only after
the measurement completes. UI (`ui/*.cs`) is headless-measurement-safe and can proceed in parallel.

## Failure catalog (empirical — observed 2026-06-21, this is the hardening backlog)

| # | incident (what actually happened) | root cause | countermeasure | regression test |
|---|---|---|---|---|
| F1 | Edge ballooned to ~2.9 GB → 150s no-progress → watchdog hard-reset → re-wedge loop; large-N stuck at cap 0 | effort=auto side-pages + 20 heavy django tabs under sustained load on a 16 GB box; recycle cap 1500 MB not keeping up | **RAM-aware admission**: auto-lower conc as free RAM drops; **Edge memory governor**: recycle proactively below cap, suppress side-pages under pressure, periodic clean restart every N instances | soak: drive load until Edge > cap, assert governor recycles and throughput holds |
| F2 | Disk floor: C: free hit the 7 GB floor → solve aborted chunk 1 instantly | `.fleet/swe/work` blobless clones + worktrees accumulate unbounded | **disk admission**: cap total work/ size, LRU-evict reconstructable clones, fail-soft (skip+retry) not abort | soak: fill disk near floor, assert eviction + run continues |
| F3 | **0-capture INFRA-ABORT wrongly burned 200 fresh instances + emitted a keep verdict** | loop.validate gated/burned even when solve captured nothing (disk abort writes a "done" marker) | FIXED: infra-abort guard (`65d1e42`) — no grade/gate/burn on 0 capture. Generalize: route ALL infra outcomes through `guards.classify_outcome` everywhere, never into results | unit: 0-capture → status infra_abort, registry unchanged (done) |
| F4 | Tabs on `?redirfrom=CsrToSSR&auth=2` misread as "SSO expired" → measurement wrongly killed twice | no robust auth-state detection; URL param ≠ auth state. The tabs were actually authed (chat UI loaded) | **auth-state classifier** (productionize the playwright probe): authed-chat-ready vs needs-signin vs mid-redirect → auto-renav on redirect, surface ONLY true signin | soak: expire/redirect a tab, assert classifier distinguishes + auto-renav recovers |
| F5 | `tasklist /FI "PID eq"` reported false "process died" (venv python.exe is a shim → real pid differs) → 3 concurrent orchestrators spawned + collided | PID-filter liveness on a shim pid | adopt **`guards.proc_alive`** (CIM/psutil cmdline match) everywhere liveness is checked | unit (done in guards); adopt-site audit |
| F6 | A detached child of an already-detached driver got reaped mid-run | nested detach orphaning | **`guards.launch_detached`** for top-level; blocking children otherwise (loop fixed) | covered by loop smoke |
| F7 | Watchdog 150s threshold blunt: one hard-reset → re-wedge, no escalation | single-blunt-instrument recovery | **watchdog v2**: distinguish slow-agent-turn (status changing) from true wedge (status frozen); escalation ladder renav → tab-reset → Edge hard-reset → clean restart; threshold tuned to agent-turn p95 | soak: inject each wedge class, assert correct rung fires |
| F8 | User could not tell "done" vs "staging gap" vs "wedged" | no structured health signal; inter-chunk staging looks idle | **structured health** (Edge mem / free RAM / disk / conc / resets / auth-state / phase) surfaced in the cockpit (ties to UX Pillar 4) | n/a (observability) |

## Hardening pillars (countermeasures grouped)

1. **Resource admission & auto-tuning** (F1, F2): RAM-linked concurrency, Edge memory governor,
   disk admission with LRU eviction. (cf. project_fleet_admission_design — continuous capacity-aware
   admission.)
2. **Edge/Copilot session robustness** (F4, F7): auth-state classifier + auto-renav; watchdog v2 with
   an escalation ladder; send/response robustness on redirect-variant DOMs.
3. **Process & outcome discipline** (F3, F5, F6): adopt `guards.proc_alive` / `launch_detached` /
   `classify_outcome` across fleet + orchestrator — infra never reflected into results/scaffold.
4. **Observability & proof** (F8): structured health surface + a **soak/chaos harness** that injects
   F1–F7 and asserts auto-recovery — turning today's firefights into regression tests. "Fixed" is
   only claimed once soak is green.

## UI/UX roadmap (from the 2026-06-21 assessment of FleetCockpit.cs / CopilotChat.cs)

Current state (better than expected — not starting from zero): WPF-native. **FleetCockpit** = live
virtualized N-worker grid (goal/status/turn/verify/reason per worker), per-worker release/retry/steer,
fleet pause/resume/stop, effort + approval modes, RAM-gated max-tabs, slash-command goal input, view
filters; driven by `.fleet/status.json` (700 ms poll) + `.fleet/commands.json` (control writes).
**CopilotChat** = SSE-streaming chat, conversation sidebar, fleet-snapshot view (transcript + live
status tail, steerable), slash palette, attachments.

**Measurement-safety split (decides ORDER):**
- Most **Tier-1 parity** items need the FLEET to write NEW `status.json` fields (streamed `last`
  chunks, `latest_diff`, `completed_steps`, `current_action`, `approval_pending`) -> that edits
  `fleet_runner.py` = measurement-active -> **defer the fleet-side writes to after the run**; the UI
  renderers can be prepped against a documented schema.
- The **self-improvement dashboard** reads `relay/selfimprove/*.jsonl` + `grade_results.jsonl` +
  reports — data NOT produced by the live fleet -> **fully measurement-safe, build now**. It is also
  the headline *surpass* feature. => **build the self-improve dashboard first.**

### Tier 1 — Claude Code parity (high-ROI; fleet-write parts deferred)
- real-time streaming of each worker's output into its card (currently only a truncated snapshot)
- inline diff/patch viewer (capture git diff into status.json -> "Show diff" panel)
- task/step checklist per worker (`completed_steps[]` -> collapsible checklist)
- mid-run permission/approval prompts (`approval_pending`/`prompt_text` -> modal banner + commands.json)
- "current action" subtitle (`current_action`) — kills the F8 "is it done or staging?" confusion
- full error/logs pane; session/history tab in cockpit; chapter/timeline breadcrumb

### Tier 2 — SURPASS (build first; measurement-safe)
- **Self-improvement dashboard** (the differentiator Claude Code/Codex do not ship): pass@1 trend +
  Wilson CI bands across iterations; A/B delta (cards on/off) + McNemar p vs the significance-gate
  threshold (green/yellow/red); burned-instance ledger (sortable, click-to-inspect); proposal queue
  (overfit-lint ✓/✗, keep/revert); genome archive + QD map; A/B pair viewer (discordant helped/hurt).
  Data backbone first: a `dashboard_state()` aggregator over the selfimprove ledgers (the
  "status.json for self-improvement") that both a CLI and the WPF view consume.
- richer fleet viz (state-transition animation, dependency DAG); live goal queue with reorder.

### Tier 3 — enterprise differentiation (pure win, optional MCP gate)
- M365 Graph identity/ACLs; SharePoint/Teams approval workflows surfaced in-cockpit; M365 audit-log
  of runs; read-only web share-link of a live fleet snapshot for team collaboration.

### Known UX friction (from assessment)
two-window cockpit↔chat (consider split-pane / inline fleet tab); status.json-only data source
(consider per-worker `.fleet/detail/<w>.jsonl` or a WebSocket bridge vs 700 ms poll); steer latency
(700 ms tick).

## Sequence

1. (now, measurement-safe) this doc; UX assessment; soak/chaos harness FRAMEWORK (mocked injection,
   real chaos deferred); auth-state classifier as a NEW module.
2. (now, parallel, UI-safe) UI/UX parity + surpass features in `ui/*.cs`.
3. (after measurement) adopt guards into fleet internals; admission; Edge governor; watchdog v2 —
   each gated by the soak harness.
