# 2026-09-28 handoff: Fleet stability, durable handoff, goal display, and main CI

This is the continuation document for the long 2026-09-26 -> 2026-09-28 stabilization session in `<repo-root>`. Read this before changing Fleet / Cockpit / supervisor / command-channel code. The main goal of the session changed from feature development to restoring basic reliability after regressions: long waits, live tasks that appeared to vanish, duplicate coordinators, unsafe resume state, and unreadably long goal text.

## 0. Current repository / process state at handoff

Repository:

- Root: `<repo-root>`
- Current checked-out branch in this worktree at handoff: `fix/codeql-secret-output`
- Current HEAD: `1183e6f` (`origin/main`, `main`) -- this branch currently has no commits beyond main.
- `git status --short` was clean before this handoff document was written.
- `fix/fleet-live-admission-and-submit` is checked out in another worktree (a separate temporary worktree; inspect with `git worktree list`). Do NOT try to switch this worktree to that branch.
- More importantly, there is no reason to return to that Fleet branch for the completed fixes: `git log origin/main..origin/fix/fleet-live-admission-and-submit` is empty. Its work has been merged to main.

Processes observed at handoff:

- `scripts/supervisor.ps1` was running at capture time; always re-check the current PID.
- The two desktop UI executables were running from the repository `ui/` directory at capture time.
- No live `relay.fleet_runner` was running at the last clean completion check unless a user task has started since this document was written.

There is one pre-existing stash unrelated to the Fleet stabilization:

- `stash@{0}: On main: private audit notes before codeql38 fix`

Do not drop/apply it casually.

## 1. Delivery / merge status -- do not redo completed Fleet work

The stabilization work was delivered through two merged PRs:

- PR #47 `fix(fleet): make live task handoff crash-durable`, merge commit `a40002f`.
- PR #51 `fix(fleet): preserve task identity across display and resume`, merge commit `e429f64`.

The remote Fleet branch later synchronized main security/CI fixes and currently points at `4099997` (`merge: sync main security and CI fixes`), but it contains no commits not already reachable from main.

Important stabilization commits now reachable from main include:

- `6b1ffd8` `fix(fleet): make live task handoff crash-durable`
- `5556142` `fix(fleet): separate task identity from full goal text`
- `4b78ef2` `fix(fleet): clamp live tab capacity at apply layer`
- `44670ff` `fix(fleet): close receipt and live-submit races`
- `32e26cf` `fix(fleet): claim live commands one at a time`
- `a77e7db` `fix(fleet): join resume durability on admission jid`
- `5e3d56b` `fix(fleet): bind active markers to process birth`
- plus subsequent main security/CI fixes merged into the branch/main.

At one point branch CI at head `40999972f21fd45e6fa3da18b2014fe0ccd74673` was fully green for CI, CodeQL, Secret scan, Windows build, and install-path E2E.

## 2. Why feature work was stopped

The session had started moving toward a Codex/C2C-like long-running durable runtime for a M365 Copilot companion. That work was deliberately paused when the existing Fleet became unreliable. The user explicitly set the priority: a system that currently loses or stalls ordinary tasks must be stabilized before adding features.

Observed user-facing failures:

- Workers remained in `waiting` for many minutes (example screenshot used during diagnosis: `スクリーンショット 2026-09-26 205120.png`; re-locate it locally if visual comparison is needed).
- A run launched with one goal could show only one effective lane even after many tasks were added later.
- Text entered into the bottom composer while a run was active sometimes did not become a new task.
- Live task submissions could race run shutdown and remain unconsumed.
- Multiple `fleet_runner` processes were observed simultaneously on the same `.fleet` state.
- Resume ledgers could omit live-added work or forget DONE work.
- Some worker turns had a reply recorded quickly but still sat until the outer timeout and got resent.
- Goal/directive text became thousands of characters long, making the Cockpit unreadable.

The fixes below were driven by measured live state, not by speculative refactoring.

## 3. Root cause: live capacity was capped by the launch-time goal count

Measured behavior:

- User configuration was not the problem: effective settings included `maxtabs=11`, `autoscale=1`, `autoscale_max=30`.
- The runner started with one goal and computed capacity using the launch queue length (e.g. patterns equivalent to `min(settings_maxtabs(), len(goals))`, `auto_concurrency(len(goals))`, and a ceiling clamped to `len(goals)`).
- When later `add_goal` commands grew the run to many workers, the concurrency budget remained effectively one lane.
- Live mitigation via autoscale command immediately increased the effective capacity and waiting workers started moving, confirming the diagnosis.

Permanent behavior now required:

- Capacity describes machine/operator limits, not the number of goals present at t=0.
- The pending queue itself limits actual starts when there is only one task.
- Later live-added tasks may immediately consume otherwise-idle lanes subject to RAM/disk/admission limits.

Key tests include the live-added-capacity regression tests added during stabilization. Do not reintroduce `len(goals)` as a permanent concurrency ceiling.

## 4. Root cause: the bottom composer lied about what it did

The UI placeholder said variants of "Add tasks...", but while a run was active both the main button and Ctrl+Enter called the bottom-composer steer path (`TrySendSteer`) instead of enqueuing new work.

Effect:

- A user entered what they believed was a new task.
- The text was consumed as an intervention/steer for an existing worker, so no new worker appeared.

Permanent UI contract:

- The bottom composer is task intake both when idle and while a run is active.
- Idle: it starts a run.
- Active: it adds new goals to the active run.
- Worker-specific steering remains available on worker cards; it is not multiplexed through the task-add box.
- Slash settings still take precedence when the input begins with a recognized command.

Relevant UI tests were updated to enforce this distinction.

## 5. Root cause: replied turns were retried as timeouts

This was measured from real transcripts, not inferred.

Example from run `r6ab7a384_a0`, worker W4:

- A turn produced an assistant response around +16 seconds and the response was already in the transcript.
- The same turn later reached about +240.6 seconds and was treated as a timeout/retry.
- The pre-fix `waiting` poll checked the outer timeout before it checked whether a new answer block already existed.
- Thus one missed/late poll could hit the deadline and resend even though a reply was already present.

A prior recorded run showed the same class of failure (reply around +70s, timeout around +240s).

Permanent contract:

- "A reply that already exists beats the outer timeout."
- The worker must first detect whether a new answer exists.
- If a reply exists, do not timeout/retry it; let the normal generating/stale/settle gates decide when it is safe to consume.
- Only a turn with no new reply may enter the outer timeout/retry path.

This avoids both wasted minutes and duplicate action risk.

## 6. Socket / wait timing: why a 1200-second number appeared

The screenshot / status text showed wait budgets up to 1200 seconds. This was not simply an old configuration accidentally left in memory.

- The socket path had an intentionally large turn timeout (1200 s).
- Ordinary tab/per-turn bounds were much lower (e.g. ~240 s in the investigated path) and generation-wait budget around ~360 s.
- There was also a meaningful-progress/no-progress mechanism intended to break a bad socket path sooner.

The more serious problem was that timeouts fed into transient retry behavior and could repeatedly resend/retry; combined with missed reply recognition this made real waits far longer than model latency.

Do not "fix" this only by changing 1200 to a smaller magic number. Preserve the measured-reply-first semantics and inspect retry ownership/meaningful-progress gates when changing these timers.

## 7. Root cause: live-added goals were absent from the durable resume ledger

Before the fix:

- `last_run_goals.json` was written once at launch.
- `add_goal` accepted later only appended to the in-memory `add_box`/pending queue.
- DONE data for live-added workers could exist while the corresponding goal was absent from `last_run_goals.json`.
- After a crash/restart, `--resume` therefore could not reconstruct all work that the UI had already told the user was accepted.

Permanent contract:

- A live `add_goal` must be durably added to the current run ledger before it becomes ordinary in-memory work.
- Stable goal keys make re-delivery idempotent.
- For a command containing multiple goals, persist the whole accepted batch before exposing any of it to the in-memory queue.

During the live repair, the damaged current ledger was backed up and rebuilt from visible worker/status/history data before attempting a handoff.

## 8. Root cause: graceful stop could erase DONE history

Observed live behavior:

- Before graceful stop, DONE results existed in `last_run_done.json`.
- Finalization then rewrote `last_run_done.json` from only the final `results` chunk.
- On a stop/reconnect path that final chunk did not contain the whole run, so the file could become `{}` and `--resume` would replay already-completed work.

Permanent contract:

- Finalization must merge successful outcomes into the durable done map; it must not replace the file from one partial reconnect/stop chunk.
- DONE keys are monotonic for the life of a run ledger.

The recovery during the incident reconstructed DONE keys from `history.json` for run `r6ab7a384_a0` before resuming the one truly unfinished goal.

## 9. Root cause: more than one coordinator could own the same state directory

This was observed directly: three `relay.fleet_runner --resume ...` processes existed concurrently against the same `.fleet`. Their status / active-marker ownership disagreed.

Outer launchers (supervisor, start_all, Cockpit) already had guards, but those are not sufficient. The process that mutates the state directory must enforce the invariant itself.

Permanent design now:

1. Live active-marker check protects migration from older runners.
2. An OS byte-range file lock (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) is acquired non-blocking per state directory.
3. The lock is acquired immediately after argument parsing, before queue receipts, retention, resume expansion, goal/done ledger rewrites, or active-marker writes.
4. If another coordinator owns the marker/lock, the second process exits with a real nonzero code (3) and must not mutate state.
5. Kernel locks release when a process dies, avoiding stale lockfiles.

A real duplicate-launch test was performed: the second process refused immediately, returned exit code 3, left the live runner alone, and did not modify goal/done/marker state.

## 10. Root cause: an old runner could delete a new runner's active marker

Old `_clear_active_marker()` simply removed the path with no ownership check.

Failure sequence observed / matched:

- Runner A entered cleanup.
- Runner B started and wrote a fresh `fleet_run_active.json`.
- Runner A finished later and unconditionally deleted the file now owned by B.
- External resume logic saw no marker and could start another coordinator.

Permanent contract:

- Active-marker deletion is owner-aware.
- A runner may clear the marker only if the marker still identifies that runner (later hardened further by process-birth binding in `5e3d56b`).
- Never restore unconditional file deletion here.

## 11. Root cause: the command channel deleted before durable application

Old production behavior called `read_commands()`; reading removed the command JSON, and `_apply_command` happened afterwards.

Crash hole:

1. `commands.d/<id>.json` exists.
2. Runner reads/deletes it.
3. Process crashes before `add_goal` is written to the live goals ledger.
4. User saw "queued", but both command and durable goal are gone.

Permanent command lifecycle:

- Atomic claim: rename `*.json` -> `*.json.claim-PID`.
- Apply/validate.
- For `add_goal`, durable ledger effects must exist before commit.
- Commit: rename to an `.applied` tombstone / write applied receipt, then remove housekeeping artifact.
- If process dies before commit, the next coordinator recovers claims whose owner PID is dead.
- An `.applied` tombstone is never replayed.
- Invalid commands are terminally consumed with a rejected receipt.
- Application failure before durable commit restores the claim for retry.

`read_commands()` remains as a compatibility seam for tests/tools, but the production live drain uses claim -> apply -> commit.

## 12. Root cause: live task submission could race run shutdown

This reproduced in an E2E smoke:

- Smoke A was so fast that the runner completed.
- A live-add command for smoke B was written immediately around shutdown.
- No runner remained to consume the new JSON.
- The command survived in `commands.d`, proving the task had not vanished from disk, but the UI had no consumer and showed the user's symptom: "I submitted it but it did not appear."

Permanent handoff design:

- Cockpit uses a tracked command write for live `add_goal`, obtaining the exact command file path.
- It adds a landing/apply receipt path (`ack`).
- The runner writes `applied:true` only after durable application.
- If the run ends with no applied receipt, Cockpit launches a rescue runner with `--adopt-command <exact pending json>`.
- `--adopt-command` is processed only after that rescue runner wins the state-dir OS lock.
- A rescue runner that loses ownership exits without touching the command.
- Adoption accepts only `add_goal` (+ optional ack), never live control commands such as stop/steer/settings.
- The adopted command is not committed/deleted until a durable goals ledger and active interruption marker both exist.
- The rescue path retries rather than firing once, because a dying previous coordinator may hold the OS lock briefly.

Real isolated E2E completed this path successfully:

`commands.d -> --adopt-command -> applied ack -> worker DONE -> done map -> active marker cleanup`

and left zero pending command JSON files.

## 13. Goal/directive text was 2k-3.5k+ characters and unreadable -- fixed

Measured recent archived goals commonly had lengths around 1,900 to 3,500+ characters. The old Directive band joined full goal text for all active goals, so the operator could no longer tell what each worker was actually doing.

Do NOT solve this by truncating the execution goal itself.

Current separation:

- `goal`: authoritative full instruction, retained for execution/retry/resume/details.
- `directive`: authoritative full directive, retained.
- `goal_summary`: compact display-only task identity.
- `directive_summary`: compact display-only directive identity.
- `run_label`: same compact display concept.

The display summary reuses `relay.conv_title.make_title`, which is deterministic/extractive (no LLM call and no invented outcome), bounded, and display-redacted. It was strengthened so policy-only openings such as `READ-ONLY audit only; do not edit...` do not hide the actual requested task; it advances to a task-identifying clause such as "Review..." / "Thoroughly investigate...".

Cockpit behavior:

- Directive band shows one compact summary per distinct full goal.
- Card headlines prefer `goal_summary`.
- History rows persist and prefer `goal_summary`.
- Old history rows retain fallback behavior.
- Expanded Overview still shows the full authoritative `goal`.

Validation at the time this was introduced: 311 related pytest cases green, legacy fleet-runner fix harness 61/61, both WPF binaries rebuilt/restarted successfully.

## 14. Test / validation highlights from the stabilization

Do not interpret these counts as a substitute for current CI; they describe what was run locally while fixing the incidents.

- Targeted live capacity / UI intake tests: green after red-first reproduction.
- Reply-before-timeout tests: green; timeout/no-reply behavior still preserved.
- Resume / done-map tests: green.
- Single-instance tests plus real duplicate-process launch: green.
- Command claim/commit tests included a real subprocess that claims then exits without commit; next process successfully recovered the dead owner's claim.
- Tracked live-add / adopt-command tests: green.
- Related integration suite reached 201 green at one checkpoint.
- Goal summary + durability regression suite reached 311 green at another checkpoint.
- Legacy manual `relay/test_fleet_runner_fixes.py` harness passed 61/61 after the goal-title contract update.
- Real isolated adopt-command E2E completed successfully.
- Both desktop UI executables were rebuilt and running after the changes.

CI manifest was also updated when newly-added tests initially caused CI-manifest failure. The missing tests were formally added rather than ignored.

## 15. User-visible priorities / constraints for continued work

The user explicitly prefers:

- Stabilize existing behavior before adding new features.
- Do not waste time making them manually run PowerShell when local connector access works.
- Use the local connector to inspect/modify/test directly.
- Commit/push at sensible atomic boundaries so CI, CodeQL and code scanning can run; do NOT blindly commit/push every incidental change.
- If using the local Copilot agent/fleet for delegated work, submit via the GUI-visible route described in `docs/research/fleet_operation_routes.md`. The user explicitly asked that Copilot-agent jobs appear in the GUI.
- Long-running research is a good candidate for the local Copilot agent, which can loop for hours.
- Avoid turning explanations into gratuitous bullet-list walls in normal user-facing replies; repository handoff documents can remain structured because they are operational references.

## 16. Main CI is currently red -- exact latest failure at handoff

Main latest inspected CI run:

- Head: `1183e6fd8e0a620612bd3b4d9ee85e3d8287764c` (merge PR #58)
- Run ID: `36374587544`
- Job `test` failed in `Run hermetic unit tests`.
- Summary: `3 failed, 9177 passed, 1623 skipped` in ~18m35s.
- Other main workflows at the same head were green: Windows build, Workflow lint, Secret scan, Install path, CodeQL.

The three failing tests / causes:

### 16.1 Child-process output decode guard: two new call sites

`tools/test_child_output_is_not_decoded_by_luck.py` reports two unlisted risky call sites in:

- `ui/test_stale_submitted_tasks_are_observable.py::0806fab93161`
- `ui/test_stale_submitted_tasks_are_observable.py::6674ad39486b`

Failure text says these calls decode child output through the local code page (cp932 on the source Windows environment) and can lose the entire output on one bad byte. The expected repair is NOT to add these fingerprints to the baseline. Change that test/helper code to use `tools.childproc.run` or `tools.childproc.decode` (whichever matches the local pattern), then confirm the risky-site inventory has no new entries.

Two tests fail from the same underlying issue:

- `test_no_new_call_decodes_child_output_by_luck`
- `test_a_listed_file_cannot_swap_one_violation_for_another`

### 16.2 Stale indicator contract regressed

`tools/test_a_dot_on_in_the_normal_state_is_not_a_signal.py::test_the_dot_is_not_amber_merely_for_being_stale`

expects the Cockpit to require:

`codeState == "stale" && StaleLongEnoughToMatter(srvBody)`

The test says the amber branch no longer requires the stale duration gate. Inspect current `ui/FleetCockpit.cs` around the code-state / health-dot logic. Restore the duration gate rather than weakening the test unless live product semantics clearly changed and a replacement invariant is stronger.

There is already a branch/worktree history around main CI stale/decoding fixes (`fix/main-ci-stale-and-decoding` etc.). Before duplicating work, inspect whether those commits were merged, superseded, or can be reapplied cleanly to current main.

## 17. Recommended next actions, in order

1. Finish the current main CI repair first because main is red while security/build workflows are otherwise green.
   - Fix the two unsafe child-output decode call sites in `ui/test_stale_submitted_tasks_are_observable.py` using the canonical `tools.childproc` helper.
   - Restore/verify the stale-duration gate in `FleetCockpit.cs`.
   - Run the three failing tests locally first, then the relevant childproc/stale UI test clusters.
   - Commit as an atomic main-CI repair and push; observe CI.

2. After main CI is green, inspect current code scanning alerts / CodeQL only if fresh alerts exist. At handoff CodeQL and Secret scan were green on main head `1183e6f`.

3. Only after baseline health is stable, resume broader durable-companion/C2C-like feature work. The durable runtime feature itself has already been merged through the later main history (PR #55 was merged before current main). Do not recreate the earlier phase-1 code from scratch without inspecting main first.

4. Continue monitoring the original user-facing metrics during real use:
   - time from live submit to worker row / applied ack,
   - waiting duration vs actual model reply timestamp,
   - number of `fleet_runner` processes per state dir (must be <=1),
   - presence/ownership of `fleet_run_active.json`,
   - pending/dead claim files,
   - resume goal/done ledger consistency.

5. If a live task "doesn't appear" again, first inspect `commands.d`, `acks`, goal ledger, active marker, runner process count, and status. Do not immediately add retries or increase timeouts; this session demonstrated several distinct causes that look identical in the UI.

## 18. Files / areas most relevant to continuation

Core runner / durability:

- `relay/fleet_runner.py`
- `relay/relay_fleet.py`
- `relay/task_router.py`
- `relay/conv_title.py`

UI:

- `ui/FleetCockpit.cs`
- `ui/FleetCommands.cs`
- `ui/CopilotChat.cs`
- `ui/rebuild_ui.ps1`

Supervision / startup:

- `scripts/supervisor.ps1`
- `scripts/start_all.ps1`
- `scripts/win/resume_interrupted_fleet.py`

Operational docs:

- `docs/research/fleet_operation_routes.md`
- `docs/private/20260926_fleet_usage_notes.md` (if present in current main; it was updated during stabilization)
- this file: `docs/private/20260928_stabilization_handoff.md`

Representative regression tests added/used:

- `relay/test_live_added_goals_keep_capacity.py`
- `ui/test_live_composer_adds_tasks.py`
- `relay/test_reply_beats_timeout.py`
- `relay/test_fleet_resume.py`
- `relay/test_fleet_runner_single_instance.py`
- `relay/test_command_claim_commit.py`
- `relay/test_adopt_pending_command.py`
- `ui/test_live_add_survives_runner_shutdown.py`
- `relay/test_goal_display_summary.py`
- `ui/test_goal_summary_display.py`
- `ui/test_every_lane_in_flight_is_on_screen.py`

## 19. Safety / operational cautions

- Do not kill a live fleet runner merely because it is slow without first checking whether it has an applied reply / durable command state. Earlier in the incident, killing before ledger repair could have lost live-added goals.
- Do not manually delete active markers as a routine repair; ownership and process-birth checks now exist for a reason.
- Do not turn the state-dir lock into a plain "lock file exists" check. Use the kernel lock; plain files go stale after crashes.
- Do not weaken receipt semantics back to "file was read" for production live add. `applied:true` is the meaningful boundary.
- Do not add new child-process decode exceptions to a baseline just to make CI green. Main currently demonstrates why that guard exists.
- Do not remove full goals from state/history in pursuit of shorter UI. Short display fields and full execution fields are intentionally separate.

## 20. One-sentence continuation context

The Fleet reliability work is merged to main; the next immediate task is to repair the three current main CI regressions (two unsafe child-output decode call sites in `ui/test_stale_submitted_tasks_are_observable.py` and the missing stale-duration health-dot gate in `FleetCockpit.cs`), then re-run/push CI before resuming feature development.