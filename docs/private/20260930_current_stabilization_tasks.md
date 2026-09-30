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
2. ACTIVE -- STAB-004: the 240s blind-wait defect is repaired to consult meaningful-idle at 90s, but 90s is only an intermediate ceiling. User requirement is to minimize waiting as far as safely possible. Measure what `generation_idle_s` actually means, distinguish healthy long computation from transport silence, then drive the safe recovery latency down to the smallest evidence-supported value without causing false reconnects or duplicate work.
3. ACTIVE -- STAB-003 foreground console regression: user reconfirmed shell/cmd still appears in foreground during CopilotAgent open/submit. Reproduce with process-tree/window-owner capture and remove the remaining visible-console spawn path; static `CreateNoWindow` evidence is not sufficient.
4. ACTIVE -- unlock false-positive investigation: recent transcripts repeatedly contain responses equivalent to `no tool was executed, therefore unlock is unnecessary`; Fleet then sees unlock/lock marker words and promotes the response into lock suspicion / intervention. Distinguish an explicit `unlock not required` / read-only/no-tool response from an actual write/tool lock refusal before escalating to unlock probe or needs-attention.
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
Status: PATCH COMMITTED / PUSHED (`540ebdb`) / LIVE VISUAL VERIFY PENDING

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

### STAB-003 -- foreground PowerShell / cmd window when CopilotAgent opens or work is submitted
Status: REOPENED / LIVE REGRESSION CONFIRMED BY USER

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
Status: REOPENED / IN PROGRESS

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

### STAB-005 -- task entered/submitted but not reflected in Fleet UI
Status: REOPENED / VERIFY CURRENT PATH

Past fixes covered bottom-composer steer-vs-add confusion, live add durability, applied receipts, shutdown handoff, and GUI text verification. The user reports current regressions have increased, so historical green tests are not enough.

Acceptance:
- GUI-visible submission is immediately represented as accepted/pending before worker creation;
- exact submitted text reaches the intended task path;
- applied receipt/worker row/history reconcile without silent disappearance;
- closing-run and durable-runtime paths are included in live validation.

### STAB-006 -- server health red/yellow behavior must be truthful
Status: PARTIALLY FIXED / LIVE RE-VERIFY

Relevant current branch work:
- `7d59d47` planned server restart distinction;
- `37103eb` planned restart expiry aligned with supervisor budget;
- `f960a98` corrected `ServerTransitionPath` initialization so the planned-restart marker is actually publishable under `.fleet`.

The user previously reported server red/yellow and asked for cause + correction. Source/tests alone do not close this item; verify current live behavior through an intentional planned restart and an actual/unplanned failure signal without manufacturing an outage on the active work session.

Acceptance:
- planned restart renders the intended transitional/yellow state rather than false red;
- real server failure cannot be masked indefinitely by a stale marker;
- marker path/expiry/ownership are visible in evidence.

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
