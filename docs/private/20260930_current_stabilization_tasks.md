# 2026-09-30 current stabilization task ledger

This file is the CURRENT operational task authority for the `<companion-repo>` work after the 2026-09-29/30 session. Older handoff/audit files remain evidence, not the live priority list.

## Operating rule -- mandatory

Every material user-requested change, regression, newly discovered blocker, implementation, validation result, commit, push, CI/CodeQL result, or status reversal MUST update this ledger in the same work session. Do not continue by memory or by an older handoff after the user's requested behavior changes.

Mid-stream user interruptions are FIRST-CLASS TASK INPUT, not chat-only context. On every interruption that changes/extends the work:
1. record the new instruction in this ledger immediately, before doing substantial new implementation;
2. record how it changes priority/acceptance and the exact resume point of the interrupted work;
3. execute the new instruction;
4. automatically resume the interrupted work without waiting for another `continue`/`続けて` from the user;
5. when the interruption reveals a durable operating rule (for example branch convergence, GUI-visible submission, or evidence requirements), update the rule section as well as the individual task.

A user should never need to remind the agent to resume a previously active task merely because they supplied an additional instruction in the middle of it.

While any P0 regression below is OPEN / REOPENED / IN PROGRESS, do not resume unrelated C2C/durable-runtime feature development. Stabilize the product the user is actually running first.

CopilotAgent delegated work must use the GUI-visible route. Long-running investigation may be delegated, but the submission must remain visible in the product UI. Commit/push at meaningful atomic boundaries so CI, Windows build, CodeQL, Secret scan, PowerShell lint and workflow checks can run; do not blindly commit incidental files.

## Current worktree snapshot

- branch: `fix/phase2-audit-followups-20260929`
- HEAD at ledger creation: `f960a98` (`fix(health): root planned restart signal path`)
- current tracked modifications not yet committed:
  - `relay/transport_policy.py`
  - `relay/test_resend_checkable.py`
- current untracked path observed: `reviews/`
- do not stage/overwrite those incidentally while working another item.
- `origin/main`: `b235e01` (merged PR #66 baseline)
- PR #66 head `16c8c29` passed CI, Windows build, install path, CodeQL, Secret scan, PowerShell lint and Workflow lint before merge.

## CONVERGENCE / MAIN INTEGRATION -- binding plan

Status: IN PROGRESS / DO NOT CREATE ANOTHER STABILIZATION BRANCH

Current stabilization work converges through `fix/phase2-audit-followups-20260929` / PR #67 into `main`. Do not create another stabilization branch unless PR #67 becomes technically unusable; if an exceptional split is required, record the reason here before creating it.

Merge gate for PR #67:
1. finish the current P0 regressions on THIS branch: combined left `内容詳細` + `実行タイムライン`, socket long-wait/watchdog placement, and unlock/no-tool false-positive handling;
2. run focused regressions plus CI manifest; rebuild Windows UI whenever UI source changes;
3. commit/push each meaningful atomic repair to PR #67 so CI/Windows/CodeQL/Secret scan/PowerShell lint/Workflow lint can run;
4. once current-head checks are green and P0 acceptance is met, merge PR #67 to `main` promptly rather than continuing unrelated feature work on the branch;
5. verify post-merge `main` workflows, then reconcile old branches/PRs by patch equivalence. Close/delete only those whose unique changes are already merged or explicitly superseded; do not mass-delete by branch name.

Current convergence snapshot (2026-09-30): `origin/main` = `b235e01`; PR #67 remote head before the current uncommitted fixes = `fb21c93`. The local worktree currently has tracked edits in `ui/FleetCockpit.cs` (combined details+timeline work) and `relay/test_socket_route.py` (socket waiting watchdog regression tests); untracked `reviews/` remains intentionally untouched.

## CURRENT MID-STREAM INSTRUCTION / RESUME QUEUE

This section is updated immediately when the user interrupts ongoing work. It is the resume authority after the interrupt is handled.

1. ACTIVE -- STAB-002 latest UX requirement: the left 220px Spine must contain BOTH `内容詳細` and `実行タイムライン`. Keep the useful content-detail implementation; restore timeline in the same left frame using the historical/shared `phase_events` / `BuildTimelineEvents` contract. Expanded-card Timeline remains as detailed evidence.
2. CLOSED -- STAB-004: the 240s blind-wait defect is repaired to consult meaningful-idle at 90s, but 90s is only an intermediate ceiling. User requirement is to minimize waiting as far as safely possible. Measure what `generation_idle_s` actually means, distinguish healthy long computation from transport silence, then drive the safe recovery latency down to the smallest evidence-supported value without causing false reconnects or duplicate work.
3. ACTIVE -- STAB-003 foreground console regression: user reconfirmed shell/cmd still appears in foreground during CopilotAgent open/submit. Reproduce with process-tree/window-owner capture and remove the remaining visible-console spawn path; static `CreateNoWindow` evidence is not sufficient.
4. CLOSED / REAL-TRANSCRIPT REPLAY + WORKER-LEVEL VERIFIED (2026-10-01 08:xx JST) -- unlock false-positive investigation: explicit `unlock not required` / read-only responses are now negative lock evidence for the ambiguous paraphrase/fallback/probe paths. Literal server lock markers and exclusive server-attribution remain authoritative and still recover through unlock.
5. INTEGRATION -- after the active items above are green, commit/push them on PR #67, verify current-head checks, merge to `main`, verify post-merge main, then reconcile superseded old branches/PRs.

Resume rule: after any newly injected user instruction is handled, return automatically to the first still-ACTIVE item above. Do not wait for another user prompt.

## P0 -- restore current product behavior before feature work

### STAB-001 -- keep this ledger synchronized
Status: IN PROGRESS

Failure observed: work continued from older stabilization/audit documents while later user-requested changes were not written back. This caused real priority drift and, for the left Cockpit panel, work in the opposite direction of the current requested UX.

Acceptance:
- every item below has a current status and evidence;
- each meaningful implementation/validation/commit updates this file before moving to another topic;
- no old handoff is treated as the current task list without reconciling this file first.

### STAB-002 -- FleetCockpit left panel: combined `内容詳細` + `実行タイムライン`
Status: CLOSED / LIVE VISUALLY VERIFIED (2026-09-30 13:36 JST)

User reports that the intended/current change is to replace the execution-timeline presentation with `内容詳細`, but neither the current local source nor git history contains `内容詳細`.

Current local evidence at ledger creation:
- `ui/FleetCockpit.cs` still contains the left-panel `実行タイムライン` / `Execution timeline` surface (around current source line ~5060) and the expanded `タイムライン` / `Timeline` section (around ~14293).
- git `-S '内容詳細'` / `-S 'Content details'` over all refs returned no implementation.
- 2026-09-29 commits `053a0ff`, `d0942b8`, and `90f047b` explicitly repaired/restored timeline colors/copy, proving the live task requirement was not being tracked when that work was done.

Do not do another cosmetic timeline repair before reconciling this requirement. Preserve authoritative execution/history data; change only the operator-facing presentation required by the current UX.

Acceptance:
- the operator-facing left panel matches the intended `内容詳細` UX rather than being silently restored to the old timeline;
- tests assert the NEW contract, not the old timeline wording;
- full underlying events/history remain available where required for evidence/debugging.

2026-09-30 stabilization patch:
- the 220px left Spine no longer renders a second execution timeline; its section is now `内容詳細` / `Content details`;
- it follows `SpineFocusWorker`, so opening another worker changes the inspected task exactly as before;
- normal Fleet rows show compact task identity + status/turn/reason; durable rows additionally show execution state, current step, progress, next step, waiting reason and up to five artifacts;
- the repaint signature now tracks those content fields instead of `phase_events` count;
- authoritative `phase_events` / timeline colors / historical wording remain in the expanded card `Timeline` section, so evidence was not deleted;
- full goal text remains in the expanded Overview; the left panel uses `goal_summary` with the old `CardTitle` fallback for legacy snapshots.

Validation:
- focused/new left-spine contract: 5 passed;
- related timeline/selection/approval/left-rail set: 60 passed;
- rebuilt FleetCockpit + CopilotChat successfully; post-build related UI set: 74 passed;
- both UI processes launched from the rebuilt binaries;
- idle Fleet hides the Spine by design, so one live-task visual/UIA verification is still required before CLOSED.
2026-09-30 latest user correction (from live screenshot): `内容詳細` was a good addition but replacing the left timeline entirely was wrong. The intended left frame contains both surfaces. Historical git lineage was traced immediately: `90690ee` introduced the Evidence Spine; `25d7f1c` / `e7a3283` preserved selected-worker progress; `d639ea6` introduced durable execution progress (`current_step`, `last_progress`, `next_step`). Current implementation should combine those contracts instead of choosing one.

Updated acceptance:
- left Spine shows compact current task/content details FIRST (`goal_summary`, state/current/progress/next/waiting/artifacts where available);
- the same left Spine also shows `実行タイムライン` / `Execution timeline` using the shared historical event contract (`phase_events` first, transcript-derived fallback);
- timeline repaint tracks phase-event changes as well as content-detail changes;
- expanded-card Timeline remains detailed evidence; full Goal remains in Overview;
- live-task screenshot/visual verification required after rebuild;
- timeline labels alone are insufficient when richer worker data exists: visible entries should identify the concrete work/progress (current step / progress / reason / phase label or equivalent) without inventing details.
- 2026-09-30 implementation completed/pushed as `540ebdb` (`fix(cockpit): restore timeline beside content details`). The left Spine now keeps `内容詳細` first, restores the shared execution timeline beneath it, and adds a bounded `現在 / Now` line sourced only from `execution.current_step`, `last_progress`, then worker `reason` so timeline context identifies the concrete work instead of showing phase names alone. Related UI regressions: **61 passed**; rebuilt `FleetCockpit.exe` / `CopilotChat.exe` and both relaunched successfully. Remaining acceptance item: live-task visual screenshot/UIA verification only.
- 2026-09-30 13:36 JST live visual acceptance completed against a real running Fleet (`running=true`, three workers present). A non-focus-stealing `PrintWindow` capture of the live 1080x760 FleetCockpit showed the left Spine rendering `内容詳細` first with the focused W0 task identity, `実行中 / Turn 3/40`, wait/reason context, and then `実行タイムライン` below it with a concrete `現在:` line plus timestamped launch/running events. This is the exact combined surface required by the latest user correction; STAB-002 is now CLOSED.

### STAB-003 -- foreground PowerShell / cmd window when CopilotAgent opens or work is submitted
Status: CLOSED / LIVE POST-PATCH GUI SUBMISSION VERIFIED (2026-09-30 15:25 JST)

User reports a PowerShell or Command Prompt window still comes to the foreground, likely around CopilotAgent opening/submission.

2026-09-30 live/static evidence so far:
- C2C `execute_command` itself is **not** the direct culprit: its Windows spawn uses `windowsHide: true` in `src/system/full-access.ts`.
- 2026-09-30 12:xx JST: user explicitly reconfirmed that shell/cmd windows are STILL appearing in the foreground. Therefore the previous nested-submit repair was insufficient; do not close this item from static/process-launcher tests. Trace the actual process tree/window owner at the moment CopilotAgent opens/submits and remove the remaining visible-console launcher.
- FleetCockpit `RunPowershellScript` / reconnect / repair launchers use `UseShellExecute=false` + `CreateNoWindow=true`.
- `relay.edge_auth` and `relay.edge_recover` PowerShell launches use `childproc.headless_creationflags()`.
- a live GUI submission moved the worker `pending -> waiting` while a 12-second sample detected no persistent visible PowerShell/cmd/Windows Terminal window; this does not exclude a short flash or a different caller path.
- Windows PowerShell engine logs show **8 real `powershell.exe ... -File scripts\win\submit_via_ui.ps1` starts between 07:07 and 07:20**, immediately before the user's regression report.
- tracked automation callers still nest a fresh `powershell.exe` merely to invoke `submit_via_ui.ps1` (`run_bestofn.ps1`, `run_effort_ab.ps1`, `run_swe_via_ui.ps1`, `swe_supervisor.ps1`). A console-less/unattended parent must not create that redundant child shell.
- `McBrainLaneScaler` is a stale visible-form scheduled task but has no trigger, last ran 2026-07-04, and references a now-missing script; it is not the current 9/30 popup source.

Stabilization patch applied locally: the four tracked automation callers now invoke `submit_via_ui.ps1` **in-process** (`& <script>`) rather than spawning a nested PowerShell. `run_swe_via_ui.ps1` now maps in-process success/throw explicitly instead of relying on native-child `$LASTEXITCODE`; `swe_supervisor.ps1` maps a failed ReadOnly probe to `$false`. Human terminal examples remain separate.

Validation:
- four modified PowerShell scripts: parser errors **0/4**;
- GUI submitter + roundtrip + existing no-console-launch ratchet: **14 passed**;
- new regression catches all four pre-fix nested launch sites and is registered in blocking CI;
- still requires a live post-patch observation before this item can be called CLOSED.
- 2026-09-30 additional repair-dispatcher hardening committed/pushed as `66cce14` (`fix(windows): keep repair child processes windowless`): `scripts/repair.ps1` no longer reparses fixed PowerShell repair commands through a fresh visible `powershell.exe`. PowerShell-backed registry entries now carry structured `Script + Args` metadata and run via one `ProcessStartInfo` launcher with `UseShellExecute=false`, `CreateNoWindow=true`, `WindowStyle=Hidden`, and redirected stdout/stderr; live doctor JSON invocation uses the same hidden launcher. Focused repair tests **12 passed**, PowerShell parser clean, CI manifest clean after staging, and a `-DryRun -MockJson` dispatcher run completed exit 0 without executing the repair. Branch workflows already reported Windows build / PowerShell lint / Workflow lint / CodeQL / Secret scan / install-path green; main latest CI also finished green. This narrows the remaining STAB-003 acceptance to an actual post-patch GUI-visible submission/open observation, not more source inspection.
- 2026-09-30 15:24:55-15:25:40 JST live acceptance: a real `submit_via_ui.ps1 -Goal ...` invocation foregrounded the intended FleetCockpit and successfully started the task. In parallel, a separate `pythonw.exe` Win32 `EnumWindows` watcher sampled every **50ms for 45s** and recorded visibility transitions for `powershell.exe`, `pwsh.exe`, `cmd.exe`, `WindowsTerminal.exe`, `wt.exe`, `conhost.exe` and `OpenConsole.exe`. The watcher log contained **zero baseline-visible or became-visible shell windows**. This directly exercises the post-patch GUI-visible path and closes STAB-003 for the reproduced submit/open route; if the user sees a future recurrence, capture that exact event rather than reopening from static suspicion alone.

Existing fixes do not close this item:
- `73bf387` added repository windowless policy to several unattended PowerShell/fleet launches;
- many Cockpit durable/local-loop launches use `CreateNoWindow=true`;
- supervisor/local-loop paths also contain explicit hidden/headless launches.

Because the symptom still occurs, do not mark it resolved from source inspection. Capture the ACTUAL process/window lineage during a GUI-visible CopilotAgent launch and identify the exact unguarded launch site. `submit_via_ui.ps1` itself deliberately foregrounds/restores the FleetCockpit window; that is not proof that the shell process is hidden.

Acceptance:
- GUI-visible CopilotAgent submit/open completes with no PowerShell/cmd/Windows Terminal console brought to the foreground;
- only the intended product/browser UI may become visible;
- regression test exercises a console-less parent / real launch shape, not only a source-string assertion.

### STAB-004 -- very long waiting / apparent no-progress periods
Status: CLOSED / POST-60S LIVE VERIFIED (2026-09-30 19:xx JST)

Past Fleet fixes addressed several distinct causes (launch-time capacity clamp, reply-before-timeout, socket/retry behavior, duplicate coordinators). Current branch then added `47cf8f1` / `ccba912` class work around blind resend safety and failure visibility.

Current uncommitted finding: `goal_may_act()` treats read-only prohibitions such as `Do not edit, write, create, delete, commit, push...` as positive acting goals because it only keyword-scans. That can turn a safely repeatable read-only audit into a resend refusal/stuck path after delivery uncertainty.

Current uncommitted repair:
- `relay/transport_policy.py`: narrow conservative stripping of explicit negative-imperative clauses before action scanning;
- `relay/test_resend_checkable.py`: positive/negative/ambiguous regression cases.

Acceptance:
- focused tests green;
- read-only audit goals no longer become acting merely because they prohibit actions;
- positive/conditional/ambiguous real actions still fail closed as acting;
- live validation measures submit/reply/wait timestamps before calling this resolved.

2026-09-30 waiting-path repair:
- measured root cause reproduced: socket workers in `status == waiting` did not consult `SOCKET_MEANINGFUL_IDLE_S`; they could sit behind the generic 240s turn timeout even after the transport had already reported >90s without meaningful progress;
- extracted `_socket_meaningful_idle_stalled()` as the single transport-level guard and call it from both `_defer_generation()` and the live `waiting` poll path; tabs are explicitly excluded;
- a stalled socket becomes `ready` with `reconnect/fallback` reason without consuming the generic transient timeout retry budget;
- `relay/test_socket_route.py`: full suite **119 passed**; related timeout/resend/settle/policy set **85 passed, 2 skipped**; CI manifest **715 pytest files listed / OK**; `git diff --check` clean;
- status remains REOPENED until a live GUI-submitted task is re-measured after the new runner code is actually active;
- 90s is not the final target: quantify healthy socket `generation_idle_s` and reduce the threshold / recovery loop to the smallest safe evidence-based latency.
- 2026-09-30 current repair: removed the contradictory socket worker-count admission gate. Socket workers no longer sit PENDING behind the tab/RAM cap; request-rate pacing now occurs at the actual generative socket send, while tabs retain attach-time pacing. New `relay/test_socket_admission_no_pending.py` plus related socket/timeout/resend coverage: **170 passed, 2 skipped**; CI manifest now lists **716 pytest files**; `py_compile` and `git diff --check` clean. Committed/pushed as `b3d9ce5` (`fix(fleet): pace socket sends without pending workers`). This closes the synthetic admission regression, but live latency measurement is still required before STAB-004 closes.
- 2026-09-30 15:16 JST live-evidence review: the 13:44 GUI/Fleet transcript `r6abc7f6c_a2_w0` shows turn 1 user send -> assistant reply in **22.3s**, then turn 2 produced no assistant reply and hit the generic timeout at **240.5s**. Separately, `socket_route.jsonl` records a 13:29 socket attempt falling back at **91s meaningful idle** (`limit 90s`). These are evidence that current long waits are transport-silence dominated, but they are not enough to lower the threshold safely because healthy tool/search progress gaps were not recorded. Next measurement step: record only first crossings of 5/10/20/30/45/60/90s `generation_idle_s` per socket turn, then submit a GUI-visible read-only task and measure healthy max-idle vs stalled recovery before changing the 90s limit. The diagnostic-only `socket_idle_probe` instrumentation is now implemented locally; full `relay/test_socket_route.py` is **121 passed**, related resend/timeout/settle/policy coverage is **85 passed, 2 skipped**, CI manifest reports **722 pytest files / OK**, `py_compile` and `git diff --check` are clean.
- 2026-09-30 17:xx JST live GUI probe `r6abcab48_a0` supplied the missing threshold evidence. Across **10 socket turns / 22 first-crossing probe events**, the longest healthy continuous meaningful-idle gap was **46.165s** (`w3` turn 2), and that turn still completed `DONE` after **214.8s total**. Other healthy long turns took **120.6-124.2s total** while their continuous idle only crossed the 5s bucket, confirming that total response latency is not a stall signal when tool/search progress resets the idle clock. The one true no-reply stall (`w1` turn 2) crossed **60s and 90s continuously** before recovery. Therefore 45s is empirically unsafe, while 60s retains ~14s observed headroom and removes ~30s of the old 90s silent wait. Dedicated branch `fix/socket-idle-60s-20260930`, commit `92e1754`, lowers only the default to **60s**, preserves `MCP_FLEET_SOCKET_IDLE_S` override and keeps the 90s diagnostic bucket. Validation: `test_socket_route.py` **123 passed**; socket-adjacent **98 passed**; timeout/resend **59 passed, 2 skipped**; settle/retry/admission **142 passed**; transient script **17/17**; CI manifest **722 / OK**; `py_compile` and `git diff --check` clean. Status remains open until this patch is merged/deployed and one post-60s live task confirms early stalled-turn recovery without false recovery on a healthy long turn.
- 2026-09-30 19:xx JST post-60s live acceptance: GUI/Fleet run `r6abcdee1_a0` exercised the deployed 60s contract. Two stalled socket turns crossed the diagnostic buckets through **60.097s / 60.102s** at `limit_s=60.0` and moved to the fallback route instead of waiting for the generic 240s turn timeout. Healthy socket workers did not false-recover: `w0` finished `DONE` directly on socket in 1 turn with no fallback (max observed continuous idle **10.663s**), while `w4` finished `DONE` directly on socket after 5 turns with no fallback (max observed continuous idle **11.094s** across instrumented turns). `w3` eventually became `STUCK` only after tab fallback and 15 turns due repeated-conclusion settling, which is a separate semantic path rather than a silent-wait timeout. Combined with the earlier healthy **46.165s** continuous-idle observation, 45s remains empirically unsafe and 60s is the smallest currently evidence-supported default. STAB-004 is CLOSED; reopen only on new live evidence of false 60s recovery or a >60s healthy continuous-idle interval.

### STAB-005 -- task entered/submitted but not reflected in Fleet UI
Status: CLOSED / LIVE VERIFIED (2026-09-30 23:08 JST)

Past fixes covered bottom-composer steer-vs-add confusion, live add durability, applied receipts, shutdown handoff, and GUI text verification. The user reports current regressions have increased, so historical green tests are not enough.

Acceptance:
- GUI-visible submission is immediately represented as accepted/pending before worker creation;
- exact submitted text reaches the intended task path;
- applied receipt/worker row/history reconcile without silent disappearance;
- closing-run and durable-runtime paths are included in live validation.

### STAB-006 -- server health red/yellow behavior must be truthful
Status: CLOSED / EXACT PRODUCTION HEALTH-POLL PATH VERIFIED (2026-10-01 08:4x JST)

Relevant current branch work:
- `7d59d47` planned server restart distinction;
- `37103eb` planned restart expiry aligned with supervisor budget;
- `f960a98` corrected `ServerTransitionPath` initialization so the planned-restart marker is actually publishable under `.fleet`.

The user previously reported server red/yellow and asked for cause + correction. Source/tests alone do not close this item; verify current live behavior through an intentional planned restart and an actual/unplanned failure signal without manufacturing an outage on the active work session.

Acceptance:
- planned restart renders the intended transitional/yellow state rather than false red;
- real server failure cannot be masked indefinitely by a stale marker;
- marker path/expiry/ownership are visible in evidence.

2026-10-01 continuation evidence / hardening:
- a real supervisor-owned planned restart occurred at 07:42:16-07:42:19 JST: the supervisor logged `server is running stale code and nothing is in flight -- cycling it`, then `previous server pid=9304 ... planned: stale code cycle`, then launched the replacement; current `/health` reports `server_code=current`, `server_pid=23792`, `server_head=2dc86ba28fe4`, and `auth_fail_10m=0`;
- Fleet was not running during this verification, so no user task was interrupted;
- the normal transition budget is 240s (`StartupGraceSeconds=180` + `FailuresBeforeAction=4` * `IntervalSeconds=15`) with a 600s hard corruption/staleness cap on the reader;
- remaining defect found: the marker contained `supervisor_pid` but the Cockpit did not validate it, so a dead supervisor could leave a real outage yellow until expiry and PID reuse could falsely preserve ownership;
- repair: new markers also publish `supervisor_started` from the supervisor process birth. `ReadPlannedServerTransition()` now requires both PID and birth, verifies the live Windows process, and fails closed to no planned transition on missing/dead/reused/mismatched ownership;
- focused transition tests: 6 passed; health/Cockpit related set: 24 passed; PowerShell parser: 0 errors; FleetCockpit/CopilotChat rebuild: success; `git diff --check`: clean.

STAB-006 closure evidence (2026-10-01 08:4x JST): the remaining acceptance was completed through the ledger-approved **equivalent exact health-poll execution**, without stopping the production MCP server. A Windows C# harness compiles the shipping FleetCockpit source list and invokes the real `CockpitWindow.PollHealthOnce()` server-health branch under `--selftest`; the only injected inputs are the server HTTP result and a temporary `server_transition.json` path. With server-unreachable plus a fresh `planned_restart` marker owned by the live harness PID and matching process birth, the actual server dot became **Yellow** and retained the planned-restart reason. The same exact poll became **Red** when the marker was absent, named a dead PID, or reused the live PID with a mismatched birth time. Thus the owner/expiry checks do not mask a real outage, and planned supervisor-owned downtime is no longer falsely red. The production URL/path remain the defaults because both overrides are null and gated by `WindowSelfTest.Active`.

Validation: exact-poll harness **5 passed**; related planned-restart/expiry/health/operability/window-construction set **32 passed**; both WPF binaries rebuilt successfully and FleetCockpit/CopilotChat were observed running from the rebuilt executables; `git diff --check` clean. Natural planned restarts were also observed at 07:42:16-07:42:19 and 08:30:08-08:30:12 JST, but their ~3-4s gaps were shorter than the ordinary 15s Cockpit cadence, which is why exact polling was required rather than manufacturing a longer production outage.

STAB-006 is CLOSED. Reopen only if a future planned restart renders Red despite a live matching supervisor marker, or if a dead/stale/reused marker can soften a real outage to Yellow.

### STAB-007 -- finish current resend-policy work without losing the branch state
Status: VALIDATED / COMMITTED / PUSHED (`cca3295`)

The current `transport_policy.py` / `test_resend_checkable.py` change is retained as an atomic repair. It prevents explicit negative-imperative constraints in READ-ONLY prompts from falsely marking the goal as an external action while preserving fail-closed behavior for real, conditional, contrast, exception and negation-inverter actions.

Validation on 2026-09-30:
- focused resend/action-timeout set: **36 passed**;
- broader transport/resend/refute/timeout set: **88 passed, 2 skipped**;
- `git diff --check`: clean.

This validates the policy semantics locally; STAB-004 remains REOPENED until the live long-wait path is re-measured after delivery.

### STAB-008 -- baseline CI / security checks
Status: BASELINE GREEN, CURRENT-BRANCH CHECK REQUIRED AFTER NEXT PUSH

The old 2026-09-28 handoff saying "main CI is red" is stale. PR #66 head `16c8c29` completed:
- CI: success
- Windows build: success
- Install path: success
- CodeQL: success
- Secret scan: success
- PowerShell lint: success
- Workflow lint: success

For the current follow-up branch, re-check all relevant GitHub runs after each atomic push. A green historical main does not make the present branch green.`r`n`r`n2026-09-30 after push `cca3295`: CI, Windows build, Install path, CodeQL, Secret scan, PowerShell lint and Workflow lint all started and were **in progress** at the first check. Current `main` head `b235e01` shows all seven corresponding workflows **success**; the older `1183e6f` CI failure is superseded.


2026-09-30 pre-STAB-002 head `c2f2416`: PR #67 was CLEAN/MERGEABLE and CI, Windows build, Install path, CodeQL (Python + C#), Secret scan, PowerShell lint and Workflow lint were all SUCCESS. The STAB-002 patch below creates a new head and therefore requires a fresh check after push.
2026-09-30 latest `main` head `38f4f77`: CI, Windows install smoke, CodeQL, Secret scan, PowerShell lint and Workflow lint completed **SUCCESS**. The latest historical main CI failure (`75c157d`, run 36670394581) was isolated to `test_ten_clicks_200ms_apart_start_one_bringup`: all functional invariants passed (`bringups=1`, nine losers / foreground requests, no leftovers) and only one hosted-Windows leaver measured **5.08s** against the 5.0s hard tail bound. The same Windows-only suite passed on later main runs including `38f4f77`, so no product/threshold change was made from that single 80ms tail; monitor for recurrence rather than weakening the gate pre-emptively.


2026-10-01 current-head CI checkpoint:
- current `main` heads through PR #90 are green for CI, CodeQL, Secret scan, Windows build, Workflow lint, and the other corresponding workflows reported by GitHub; no present main regression was reproduced;
- PR #78 / branch head `e71a805` completed CI, Windows build, install path, CodeQL, Secret scan, PowerShell lint and Workflow lint successfully;
- local branch then merged newer `origin/main` as `2dc86ba` and is ahead of its remote branch, so this new STAB-006 commit must be pushed and the checks re-read on the resulting current head before integration.

## P1 -- follow-up after P0 stability

### STAB-101 -- GUI-visible CopilotAgent route remains mandatory
Status: ACTIVE CONSTRAINT

Use the GUI-visible submission route for delegated CopilotAgent work. Do not substitute a hidden fleet CLI submission merely because it is easier to automate.

### STAB-102 -- server / Fleet / durable status should show what it is doing, Codex/Claude-Code style
Status: PAUSED BY P0

The broader goal remains to make current work, progress and long-running autonomous execution visible. Do not add more surface/features until STAB-002 through STAB-006 are stabilized.

### STAB-103 -- C2C-like durable runtime feature expansion
Status: FROZEN BY USER UNTIL BASELINE IS RELIABLE

Existing phase-2 durable-runtime work remains in the repository; do not recreate it. Resume only after the current regressions are closed/reverified.

## Existing Phase-2 audit items not to lose

The detailed evidence remains in `docs/private/copilotagent_phase2_audit_20260929.md`. At ledger creation its remaining/accepted observations include:
- R5-NOTE-1: browser DOM submission is not fully reproducible in hermetic Linux tests;
- SEC-ISSUE-1: XFF/IP identity remains caller-supplied, mitigated by default-on unlock token requirement but not intrinsically fixed;
- S7-ISSUE-1: same-user arbitrary code execution is an architectural boundary, not cryptographically excluded by same-user file/tool gates.

These do not outrank the live P0 regressions above unless they become direct blockers.

## Update log

- 2026-09-30: ledger created after user identified task-tracking drift. Reopened `内容詳細`, foreground-console, long-wait, submission-visibility and health-signal items; recorded current uncommitted resend-policy work and corrected stale main-CI status.
- 2026-09-30: STAB-007 resend-policy repair validated: 36 focused tests green; broader related set 88 passed / 2 skipped; committed atomically and pushed as `cca3295`. Pre-commit identity guard caught a repository-specific label in this private ledger; it was replaced with a generic placeholder rather than bypassing the guard.
- 2026-09-30: STAB-003 foreground-console investigation ruled out C2C/Cockpit/Edge headless launchers, found eight recent nested `submit_via_ui.ps1` PowerShell starts and four tracked automation callers that created a redundant child shell. Those callers were converted to in-process invocation; parser 0/4 and 14 related tests green. Live re-verification remains pending.
- 2026-09-30: STAB-002 operator-facing left Spine changed from the duplicate execution timeline to `内容詳細`. Timeline evidence remains in expanded cards. New/related tests 60 green; rebuilt-binary UI set 74 green. Live-task visual verification remains pending because the current Fleet is idle and hides the Spine.
- 2026-09-30: user explicitly required interruption-safe operation. Added mandatory rule: every mid-stream instruction is recorded immediately with priority + resume point, handled, then interrupted work auto-resumes without another `continue`. Also recorded binding PR #67 -> main convergence plan and latest combined content-details + timeline requirement.

- 2026-09-30: during STAB-002 validation, `.git/config` was observed modified at 12:20 with `core.bare=true`, causing Git to stop recognizing the primary checkout as a work tree. Backed up the exact config and restored `core.bare=false`; branch/status became readable again. Root writer still requires attribution.
- 2026-09-30: user clarified that the timeline must show concrete work content, not merely exist, and that waiting latency must be minimized beyond the interim 90s meaningful-idle ceiling. STAB-002/STAB-004 acceptance and resume queue updated accordingly.

- 2026-09-30: user reconfirmed foreground shell/cmd still appears. STAB-003 reopened as a live regression; this is now a mandatory process-tree/window-owner investigation, not a static-launcher recheck. The same checkpoint records the socket admission/send-pacing repair (122 focused tests green).

- 2026-09-30: STAB-004 socket admission/send-pacing repair completed and pushed as `b3d9ce5`; 170 related tests passed, 2 skipped, CI manifest 716/OK. Live latency measurement remains open.

- 2026-09-30: STAB-002 combined Content details + Execution timeline implementation completed and pushed as `540ebdb`; 61 related UI tests green and both WPF binaries rebuilt/restarted. Live visual verification remains open.

## 2026-09-30 19:xx JST continuation / CI integration checkpoint

User instruction: continue current stabilization work and, when possible, inspect failures on `main` as well. Commit/push at meaningful boundaries so CI and code scanning actually run.

Current evidence after refresh:
- PR #67 is already MERGED; its merge-head checks were all green (CI, Windows build, install path, CodeQL Python/C#, Secret scan, PowerShell lint, Workflow lint).
- Current branch `fix/phase2-audit-followups-20260929` later gained two post-merge commits: `a050a4a` (60s silent-socket recovery) and `eb6c03c` (serialized automated GUI submissions).
- Those two commits have **zero GitHub check-runs** because the previous PR is closed and the workflow set does not create the full check matrix for this post-merge feature-branch push. This is not a green result; it is unvalidated remote state.
- `origin/main` is currently 9 commits ahead while this branch is 2 commits ahead. The branch must first converge with current main, then the same branch (do not create another stabilization branch) must be submitted as a fresh PR so current-head CI/CodeQL/Windows/secret/lint checks run.
- Latest inspected `main` workflow head is green. Historical main CI failures already recorded below were superseded by later green runs; do not weaken timing gates from a single hosted-runner tail unless recurrence is demonstrated.

Resume point after integration hygiene: STAB-004 post-60s live validation is CLOSED. Continue with STAB-005 task-submission live re-verification, then STAB-006 server-health live re-verification, followed by main integration.

### 2026-09-30 19:xx JST main convergence validation

- merged current `origin/main` (`dfa157e`) into `fix/phase2-audit-followups-20260929` as `8cab371`; no conflicts; untracked `kanazawa-trip/` and `reviews/` were not staged or modified;
- branch-vs-main product delta remains the intended post-PR#67 socket/GUI stabilization plus this private ledger checkpoint;
- focused socket / timeout / resend / GUI-submit integration regression set: **184 passed, 2 skipped**;
- CI manifest: **723 pytest files listed / OK**;
- `git diff --check origin/main...HEAD`: clean;
- next action: push this converged head and open a fresh PR from the SAME stabilization branch (PR #67 is already merged) so CI, Windows build, install path, CodeQL, Secret scan, PowerShell lint and Workflow lint run on the actual current head.

### 2026-09-30 19:5x JST STAB-005 live-add latency finding

A real GUI-visible A/B submission reproduced the current reflection-delay symptom without task loss. B (`STAB005B-20260930-195207056`) appeared first as the Cockpit optimistic submitted row while no B worker existed yet, then its exact `commands.d` JSON remained durable until the runner consumed it. The applied receipt was later written with `read=true, applied=true`, B became exact-goal worker `w1`, finished `DONE`, and the identical full goal reached `history.json`. This validates the non-loss path and the worker-before/after reconciliation contract.

New P0 performance defect discovered from the same run: B took about **57s** from live command landing to applied receipt / worker creation. Transcript timing for A shows its worker object existed around 19:52:18 but its first user send did not occur until 19:53:19 (~60.9s). During that interval `status.updated` / `on_tick` / command drain did not advance. `_begin_send` rate pacing is already non-blocking; the synchronous pre-attach `route.refresh(context, agent_url)` in `run_relay_fleet` can call the Playwright-backed token/template capture on the single fleet sweep thread. Full capture is documented at ~35s and the measured stall here was ~61s. `capture_floor` does not sleep.

Priority/acceptance change: STAB-005 is NOT closed merely because the optimistic row prevents disappearance. The runner must continue consuming live commands/status while socket token refresh/capture is in progress, or otherwise move the refresh cost off the command-draining critical path without violating Playwright thread ownership. Do not move a sync Playwright context to an arbitrary thread. Search existing independent-process/CDP/light-token mechanisms first and add a regression that command drain/status remains live across capture latency.

Safety constraint from the current live state: the run that began with the controlled A/B validation now also contains user travel-planning workers (`w2`-`w7`). Do **not** stop/kill/restart this runner or use it for closing-run race tests. Continue source/test work non-destructively and defer destructive handoff validation until the user work finishes naturally.

- 2026-09-30: STAB-005 source repair moved socket credential refresh/capture off the Fleet sweep into a windowless helper process (`relay/socket_capture_async.py`). While capture is in flight, command draining/status ticks continue and admission counts a worker as socket-only only when token+template are actually ready; otherwise it honestly budgets the ordinary tab path. `SocketRoute` now has per-agent capture revisions so a late async result cannot overwrite a newer Research/Refuter synchronous refresh, and route-close vs late-install is serialized. Focused validation so far: async capture 9 green, socket admission 3 green, socket route 123 green, targeted Research/Refuter/summary socket compatibility 8 green. Live A/B re-measurement remains required on a fresh run before STAB-005 closes.

### 2026-09-30 22:4x JST async-capture pre-commit validation

STAB-005 source repair is ready for a remote-check boundary. The blocking `route.refresh(context, agent_url)` call has been removed from the Fleet admission sweep. A windowless helper process now owns its own Playwright/CDP connection and returns only plain token/template data; the parent sweep only launches/polls that helper and continues command/status work. Admission treats a worker as socket-only only when `SocketRoute.ready(agent_url)` has a live token/template; otherwise it budgets the ordinary tab path. Per-agent capture revisions prevent late async results from overwriting newer synchronous Research/Refuter captures, and route-close/install are serialized.

Pre-commit validation on the exact dirty tree:
- `relay/test_socket_capture_async.py` + `relay/test_socket_admission_no_pending.py`: **12 passed**;
- `relay/test_socket_route.py`: **123 passed**;
- Research/Refuter/socket/timeout/resend compatibility slice: **113 passed, 2 skipped**;
- transient retry script: **17/17**;
- `py_compile` for async helper / route / fleet: green;
- CI manifest: **724 listed / OK** before staging the new test; re-run after staging is mandatory;
- `git diff --check`: clean.

This is source/test validation only. STAB-005 remains REOPENED until a fresh GUI-visible A/B submission demonstrates that command receipt / status ticks remain live while an actual capture helper is in flight.

### 2026-09-30 23:0x JST live Add-button identity regression and repair

A real GUI live-add attempt reproduced a second STAB-005 surface failure independent of Fleet command durability. At 22:57:03 the cockpit correctly reported `run in flight: True`, but `scripts/win/submit_via_ui.ps1` could not find the live `Add` button because the composer had a stable AutomationId (`goalInput`) while the Start/Add button was identified only by localized display names. The task therefore never reached the runner.

Repair:
- FleetCockpit assigns `_startBtn` the stable AutomationId `startButton`;
- GUI submitter resolves `startButton` first and keeps localized-name matching only as old-binary compatibility fallback;
- focused submit/GUI-lock/no-nested-console tests: **14 passed**;
- both WPF binaries rebuilt successfully.

Live re-check after rebuild: a real GUI submission printed `start button: found by AutomationId`. A subsequent in-flight add (`STAB005F-20260930-230221586`) reported `run in flight: True`, was accepted through that AutomationId path, and produced `applied=true` ack at 23:02:33 (`ts=1790776953.880...`), about **11.93s** after submission start. This is materially below the earlier ~57s live-add stall. It does not yet close the async-capture overlap acceptance: the preceding async capture completed at `1790776938.147...`, roughly 3.8s before this live add began. A fresh A/B test must still place B inside the actual helper-in-flight window.


### 2026-09-30 23:08 JST STAB-005 live acceptance

Fresh GUI A/B validation closed the remaining live-command critical-path question. A (`STAB005A2-20260930-230730326`) started a fresh run through `submit_via_ui.ps1`. B (`STAB005B2-20260930-230745239`) was then submitted immediately through the same visible cockpit composer and reported `run in flight: True` plus `start button: found by AutomationId`. Immediately after B submission the real `relay.socket_capture_async --worker` process tree was still alive.

Measured ordering:
- B GUI submit start: 23:07:45.240 JST;
- B durable `applied=true` ack: `ts=1790777284.1886287` / 23:08:04.191 wall time;
- async capture success: `at=1790777288.65013` / 23:08:08.652 file time;
- therefore the Fleet command drain durably applied B **4.462s before the capture helper completed**;
- the run identity (`started=1790777264.61075`) remained the same, status reached `total=2, queued=0`, and B became real worker `w1` (observed `refuting`, turn 1).

This directly disproves the old blocking behavior for the repaired path: the expensive Playwright/CDP capture can be in flight while the single Fleet sweep continues consuming and acknowledging live commands. Together with the exact-goal/ack/history durability fixes and the `startButton` AutomationId repair, STAB-005 is CLOSED. Reopen only on new live evidence of a command/status stall during async capture or a visible GUI submission that fails reconciliation.

### 2026-10-01 08:xx JST unlock/no-tool false-positive closure

The remaining resume-queue P0 around unlock semantics was reproduced directly. Before the repair, the real historical reply `unlockは予定の読み取りに不要なため実行していません。DONE` classified as locked whenever an unrelated fresh refusal record existed in the global window. A long security-review reply that merely quoted `[locked: no valid unlock token]` while explicitly saying the current read-only task required no unlock also entered the lock-probe path.

Repair in `relay/relay_fleet.py`:
- add `_explicit_unlock_not_required()` with narrow English/Japanese semantic patterns for `unlock/解錠/ロック解除` + `not required / not needed / unnecessary / 不要 / 必要ない`;
- do **not** treat `did not execute unlock` alone as negative evidence, because a genuinely blocked worker says that when password/tool access is unavailable;
- contradictory prose fails closed when a separate positive `unlock is required` statement exists;
- literal short server lock markers and exclusive server refusal attribution are evaluated before the negative-prose guard and therefore remain authoritative;
- the guard suppresses only ambiguous paraphrase/fallback classification and long-marker probe escalation.

Validation:
- new replay + worker-level regression: **18 passed**, including exact read-only/calendar wording preserved in the research transcript corpus;
- existing lock evidence / long-marker probe / session attribution / unlock injection / recovery set: **92 passed**;
- real lock paraphrase, literal `[locked ...]`, and failure-to-unlock cases remain classified as locked;
- `git diff --check`: clean.

This item is CLOSED. Reopen only if a reply that explicitly says unlock is unnecessary still enters unlock probe/injection, or if a real lock refusal is newly suppressed. The next unresolved P0 in this ledger is STAB-006's missing retained yellow-state sample during a natural planned server restart.

### 2026-10-01 08:4x JST P0 stabilization convergence checkpoint

All P0 behavior regressions listed in the current stabilization ledger are now CLOSED with the required live/exact evidence: left-panel details+timeline, foreground console suppression, long-wait recovery, live task reflection/async capture, unlock/no-tool false-positive handling, and truthful planned-restart server health. The resume queue now advances to **INTEGRATION**: validate the current branch head through blocking CI / Windows build / install path / CodeQL / Secret scan / PowerShell lint / Workflow lint, then merge/converge through the existing branch plan and verify post-merge `main`. Do not resume unrelated durable-runtime/C2C feature expansion before this integration gate is green.
