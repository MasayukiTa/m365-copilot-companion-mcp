# 2026-09-28 Work Ledger

Purpose: durable handoff log for this repository. This file is the restart point after an interrupted assistant/tool session.

## Recovery note
The first version of this ledger was accidentally truncated while normalizing trailing whitespace before the first commit. The entries below are a deliberate reconstruction from the tool/session record. No code/runtime mutation was lost; only the prose ledger was affected. Future entries are appended in a fixed format.

## Repository baseline
Action: Checked branch ancestry after discovering the checkout had returned to main.
Result: main=5c80f479ebf2cea39e846bd806df32d42e38d252. The prior fleet stabilization branch was already merged via PR #51; origin/fix/fleet-live-admission-and-submit is an ancestor of main. Current work branch created from main: fix/edge-respawn-runtime-stability.
Next: Diagnose hidden Edge respawn from current main, not from the old fleet branch.

## Main CI status
Action: Queried latest GitHub Actions runs on main.
Result: for 5c80f47, Secret scan, CodeQL, Workflow lint, and Windows build were successful; CI itself was still in progress at the time of the check. Older CI runs were mostly cancelled rather than failed, consistent with the duplicate-run cancellation change merged in PR #56.
Next: Recheck main CI after the next push/checkpoint.

## Current Edge/runtime state on this PC
Action: Enumerated Edge browser roots and listener owners.
Result: managed headless roots were long-lived rather than respawning locally: copilot-companion-edge :9222 PID 15092 created 2026-09-27 06:21:30; copilot-bridge-edge :9223 PID 9024 created 2026-09-27 06:21:42. A normal Default-profile Edge also existed. edge_keeper loops every 2s; its disk-cap pass is every 300s; sign-in pause backoff is 180s. Those do not explain a ~100s root respawn cadence.
Next: Use bridge/sign-in logs to identify the ~100s path.

## Local port 8765 collision
Action: Mapped 8765/9222/9223 listeners.
Result: port 8765 is currently owned by unrelated Python PID 8624 running `python -m http.server 8765 ...`; the bridge Python process is not present. start_bridge.ps1 defaults BridgePort to 8765. start_all.ps1 already detects “8765 held but /conv not answering” and refuses to start a second bridge, so this collision definitely breaks bridge startup on this PC but is not by itself the observed ~100s Edge restart loop.
Next: Keep this separate from the ~100s sign-in/Edge-loop root cause.

## Historical ~100s Edge loop reproduced from downloaded logs
Action: Read the two supplied/downloaded bridge logs, and extracted sign-in/Edge launch events.
Result: historical fresh-PC log repeatedly shows: Sign-in required -> HEADLESS to headed launch -> HardReset -> headless launch -> repeat. 16 sign-in surface events were found. Consecutive intervals were [109,108,110,112,119,94,111,109,182,105,77,117,110,97] seconds; median=109.5s, mean=111.4s, 12/14 intervals were 90-120s. This matches the user report “about every 100 seconds”.
Next: Determine whether the new PC still runs this pre-fix path or a new path.

## Known fix for that historical loop
Action: Located source comments and commit history for the measured 105-110s loop.
Result: commit 61409d70c4cf58d5e1d032e1608a43ce4edb9f47 (`Fix the sign-in window popping up and hiding itself in a loop`, 2026-09-25 16:35 +0900) is contained in current main. start_bridge.ps1 documents the exact fresh-PC incident. The fix changed Needs-SignIn so only positive `VERDICT: signed_in` counts as clear; `cannot_tell` remains sign-in-in-progress. Existing downloaded logs were last updated ~16:18, before this 16:35 fix, so they are pre-fix evidence and cannot prove post-fix recurrence.
Next: Fresh new-PC HEAD + current bridge evidence are required before changing behavior again.

## Current sign-in decision semantics
Action: Inspected scripts/start_bridge.ps1 and scripts/ensure_m365_signin.py.
Result: Demote-ToHeadless polls every 5s and only clears after confirmed signed-in readings; ambiguous mid-auth should keep the window up. bridge_signin_decision has a per-port surfaced latch and should return already_surfaced for one unresolved need. Existing tests explicitly encode the 2026-09-25 105-110s loop regression. Full/focused Windows integration test attempts exceeded tool timeouts (180s and 120s respectively); this was a test-runtime timeout, not an observed assertion failure.
Next: Collect post-61409d7 evidence from the new PC rather than guessing another fix.

## Bridge watchdog timing considered
Action: Inspected bridge/copilot_bridge.py watchdog and rehide timers.
Result: force-rehide default=90s, page-owner wedge limit=120s, CDP watchdog=10s x 3. Current local bridge.log has repeated owner-thread liveness warnings but only one Edge launch in the sampled current log and no keepalive-exit events, so current local evidence does not show a constant 120s watchdog restart loop. The historical fresh-PC log instead directly shows the sign-in surface/hard-reset cycle.
Next: Fresh-PC bundle must distinguish sign-in relaunch from watchdog/process recovery.

## Diagnostic collector added
Action: Added scripts/collect_edge_respawn_diagnostics.ps1.
Result: read-only collector produces one zip with git HEAD/branch, whether HEAD contains 61409d7, safe allow-listed timing env values, managed Edge roots, listener owners for 8765/9222/9223, bridge/start_all/supervisor logs, signin_surfaced_9223 latch if present, edge mode, page-count tail, and filtered bridge events. It does not kill/restart processes or dump the full environment/.env. Local execution succeeded and zip contents were inspected, then the test zip was deleted.
Next: Use this collector on a post-update new PC; for pre-update/stale-version proof, capture HEAD + logs before pulling.

## Collector tests
Action: Added two source-contract tests to scripts/test_operational_resilience.py.
Result: 2 passed, 42 deselected. PowerShell parser reports 0 errors. git diff --check was clean before the ledger formatting problem.
Next: Commit/push collector + tests + this reconstructed ledger.

## Failed checkpoint commit and ledger repair
Action: Attempted checkpoint commit.
Result: commit was intentionally aborted because `git diff --check` found trailing whitespace in the original malformed ledger formatting. A subsequent normalization command used an invalid Python newline argument and truncated only the prose ledger; source/test/runtime files were unaffected. An attempted Git-object recovery was not worth further time after tool output limits. This reconstructed ledger replaces it.
Next: Re-stage, run diff check, commit, push, then continue diagnostics.

## 2026-09-28 identity-guard checkpoint
Action: Attempted the diagnostic checkpoint commit.
Result: The pre-commit identity guard refused the commit because staged diagnostic prose/output names contained configured-name and employee-ID-shaped strings. No commit or push occurred; bypass was deliberately not used.
Next: Remove unnecessary identifying strings, re-run tests/diff/guard, then commit normally.

## 2026-09-28 10:26:46 +09:00
Action: Re-ran collector tests and PowerShell parse after identity scrubbing.
Result: pytest=0; PowerShell parse errors=0.
Next: Commit and push normally with pre-commit guard enabled.

## 2026-09-28 10:28:34 +09:00
Action: Verified commit state after an apparently contradictory commit response.
Result: Reflog proves commit 85a95ed was created at 10:26:54 with the collector, tests, and ledger. Remote branch does not yet exist; worktree was clean. The later 'nothing to commit' was only a duplicate commit attempt.
Next: Amend this note into the local checkpoint and push the branch.

## 2026-09-28 10:29:34 +09:00
Action: Verified commit state after an apparently contradictory commit response.
Result: Reflog proves commit 85a95ed was created at 10:26:54 with the collector, tests, and ledger. Remote branch does not yet exist; worktree was clean. The later 'nothing to commit' was only a duplicate commit attempt.
Next: Amend this note into the local checkpoint and push the branch.

## 2026-09-28 10:32:30 +09:00
Action: Updated the remote diagnostic branch after local ledger amend.
Result: force-with-lease succeeded against the verified remote hash; remote now points at local HEAD 7612204.
Next: Check branch CI/main CI, then continue Edge-loop diagnosis using fresh post-fix evidence.
