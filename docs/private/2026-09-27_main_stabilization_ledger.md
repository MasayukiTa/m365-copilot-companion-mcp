# Main stabilization ledger — 2026-09-27

Authoritative working checklist for the current repository stabilization. Update this file as work is completed. Do not treat a discovered issue as complete merely because a patch exists on a feature branch; production uses `main`.

## Integration state

- [x] Confirm repository: `MasayukiTa/m365-copilot-companion-mcp`.
- [x] Confirm production-relevant branch is `main`.
- [x] Fast-forward local `main` to `origin/main` (`051661f`).
- [x] Merge `fix/fleet-live-admission-and-submit` into local `main` with no conflicts, stopping before commit for validation.
- [x] Confirm only one patch-equivalent duplicate exists (`ed518f7` == `origin/main` `051661f`); all other feature commits are unique main work.
- [x] Validate merged main in bounded chunks (avoid one oversized pytest invocation that leaves duplicate validation trees).
- [x] Rebuild FleetCockpit/CopilotChat from merged main and verify deployed exe/source consistency.
- [x] Commit the merge on main (`a40002f`).
- [x] Push main.
- [x] Confirm remote main SHA matches local main (`a40002f`).

## Reliability fixes being promoted to main

- [x] Live task intake uses add-goal rather than silently steering an existing worker.
- [x] Live concurrency capacity is not permanently clamped to the launch-time goal count.
- [x] Reply already present wins before timeout retry; avoid duplicate resend after a completed reply.
- [x] Live-added goals are persisted for resume.
- [x] DONE map merges across reconnect/stop chunks instead of being replaced by the last chunk.
- [x] One coordinator per fleet state dir enforced by live-owner marker + OS lock.
- [x] Active marker deletion is owner-PID constrained.
- [x] Command channel uses claim -> durable apply -> commit; dead-owner claims are recoverable.
- [x] `--adopt-command` rescues a live-add command that lands during runner shutdown.
- [x] GUI live-add tracks an applied receipt and launches adopt rescue only when needed.
- [x] Rejected command receipts remain rejected across recovery.
- [x] Child validation/tool process trees are owned and reaped on timeout/worker close/coordinator unwind.
- [x] Acceptance cleanup handles blocking validation subprocess trees.
- [x] Candidate DONE is not lost merely because a refuter/verification turn times out.
- [x] Unlock recovery tests match the session-auth contract.
- [x] Display-only `goal_summary` / `directive_summary` / compact `run_label` are separated from the authoritative full goal text.

## Validation still required on merged main

- [x] Python compile for touched runtime modules.
- [x] CI test manifest audit.
- [x] Command durability / adopt / single-instance / resume test group.
- [x] Timeout / DONE / acceptance / unlock test group.
- [x] Child-process ownership / no-console-inheritance test group. Local: process-tree 4/4, no-console 6/6, child-output ratchet 11/11; unreached ratchet was stopped after >6 min local scan, while exact feature HEAD 443be85 has full CI success.
- [x] GUI submit / goal-summary / live-add / slash-path test group.
- [ ] Windows repeated-launch (`test_start_all_ten_clicks`) group. Local attempt was aborted because the external tool retried the same long pytest three times; exact feature HEAD `443be85` already has CI + Windows build success. Final confirmation is the new main CI run.
- [x] Manual `relay/test_fleet_runner_fixes.py` harness.
- [x] `git diff --cached --check` before merge commit.

## GitHub / CI work after main push

- [ ] Confirm CI workflow on new main SHA.
- [ ] Confirm CodeQL workflow on new main SHA.
- [ ] Confirm Secret scan workflow on new main SHA.
- [ ] Confirm Workflow lint / Windows build / PowerShell lint as applicable.
- [ ] Inspect current main CI failures that predate this merge and separate flakes from real defects.
- [ ] Fix main CI failures that are reproducible and attributable to repository code/config.

## GitHub security alerts (`MasayukiTa/m365-copilot-companion-mcp/security`)

- [ ] Inspect open Code Scanning alerts.
- [ ] Inspect open Dependabot alerts.
- [ ] Inspect open Secret Scanning alerts.
- [ ] Classify each as real / false-positive / already-fixed-on-main / dependency-only.
- [ ] Patch actionable alerts on main with tests where appropriate.
- [ ] Recheck alert state after push; do not close/dismiss alerts without evidence.

## Audit evidence currently present locally

Preserve these until reviewed; do not delete during cleanup:

- `docs/private/2026-09-27_fleet_child_process_ownership_audit.md`
- `docs/private/2026-09-27_fleet_left_pytest_processes.md`
- `docs/private/2026-09-27_fleet_process_tree_postfix_audit.md`
- `docs/private/2026-09-27_fleet_readonly_done_but_stuck.md`
- `docs/private/2026-09-27_pr47_final_reliability_audit.md`
- `docs/private/2026-09-27_pr47_postclaim_reliability_audit.md`
- `docs/private/20260927_pr47_postclaim_reliability_readonly_audit.md`

These are repository audit evidence and should be reviewed for tracking after main is stable. `docs/private/2026-09-27_shuttlescope_prod_deploy_native_stderr.md` is a ShuttleScope issue and must not be mixed into this repo's main stabilization commit without an explicit decision.

## Deferred feature work

- [ ] Durable-companion / LOCAL_LOOP feature branch work remains deferred until the current main Fleet path is demonstrably stable.
- [ ] Any further c2c-like feature development starts only after this ledger's main/CI/security stabilization items are closed.
