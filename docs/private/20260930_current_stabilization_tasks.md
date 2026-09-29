# 2026-09-30 current stabilization task ledger

This file is the CURRENT operational task authority for the `<companion-repo>` work after the 2026-09-29/30 session. Older handoff/audit files remain evidence, not the live priority list.

## Operating rule -- mandatory

Every material user-requested change, regression, newly discovered blocker, implementation, validation result, commit, push, CI/CodeQL result, or status reversal MUST update this ledger in the same work session. Do not continue by memory or by an older handoff after the user's requested behavior changes.

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

## P0 -- restore current product behavior before feature work

### STAB-001 -- keep this ledger synchronized
Status: IN PROGRESS

Failure observed: work continued from older stabilization/audit documents while later user-requested changes were not written back. This caused real priority drift and, for the left Cockpit panel, work in the opposite direction of the current requested UX.

Acceptance:
- every item below has a current status and evidence;
- each meaningful implementation/validation/commit updates this file before moving to another topic;
- no old handoff is treated as the current task list without reconciling this file first.

### STAB-002 -- FleetCockpit left panel: `実行タイムライン` -> `内容詳細`
Status: REOPENED / REQUIREMENT DRIFT

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

### STAB-003 -- foreground PowerShell / cmd window when CopilotAgent opens or work is submitted
Status: REOPENED

User reports a PowerShell or Command Prompt window still comes to the foreground, likely around CopilotAgent opening/submission.

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
Status: VALIDATED / COMMITTED

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

For the current follow-up branch, re-check all relevant GitHub runs after each atomic push. A green historical main does not make the present branch green.

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
- 2026-09-30: STAB-007 resend-policy repair validated: 36 focused tests green; broader related set 88 passed / 2 skipped; committed atomically with this ledger. Pre-commit identity guard caught a repository-specific label in this private ledger; it was replaced with a generic placeholder rather than bypassing the guard.
