# Claude work ledger (single, append-only)

Purpose: the one place where the Claude assistant records what it did, why, and what is still open. There is exactly one ledger; it always lives here.

Rules for this file:
- Append-only. Newest section at the bottom. Headings: `## 2026-09-30 HH:MM JST - title`.
- Update it BEFORE moving to the next topic (before leaving, not after).
- Never edit codex's ledger `docs/private/20260930_current_stabilization_tasks.md`.
- Design docs are referenced, not duplicated. Tool READMEs outside the repo are referenced by path, e.g. `%USERPROFILE%\.claude\tools\<tool>\README.md`.
- No secrets, no company names, no employee IDs, no user-name paths.

## Standing rules (rules 常時ルール)

- Never delete Windows logs of any kind.
- Never launch Outlook (COM, exe, or attaching to an existing instance).
- The hidden Edge must stay hidden: no visible mode exists.
- The product must work on a brand-new general-user PC: no admin, no optional Windows features. Verification only via quickstart.bat / start_all.bat.
- Merges are done autonomously after read-only checks (no overlap with uncommitted changes, no conflicts, CI green) and checked afterwards (core.bare is false, git status works).
- Never run `git config` in the live tree.
- Disk cleanup is delegated and deletes only owner-approved items.

## Work index

## 2026-09-25 00:00 JST - fixes on main

- Unlock password is no longer injected proactively into turn 1 of fresh conversations (Copilot's own safety filter refused it deterministically); replay/recycle paths too. Commits 5fdcd1c, 40fdc96.
- Bridge sign-in wall is recorded at startup before its page is closed.
- Sign-in window open/close loop fixed: Needs-SignIn now requires a positive signed_in verdict. 61409d7.
- Doctor bridge check retried. 8c746ea.
- start_all leaver timing root cause: WMI parent lookup skipped when the launcher already knows the parent. dd9b3b2.
- A date-hardcoded doctor test fixed. 3cb6db7.

## 2026-09-29 00:00 JST - background automation tools (outside the repo)

Under `%USERPROFILE%\.claude\tools\<tool>\README.md`:
- bgedge: private headless Edge over CDP, no visible mode, post-command visible-window invariant, refuses production ports 9222/9223/9224/8000/8765; silent OS SSO reaches M365 with sync off. 497 tests.
- bgdesk: private never-displayed desktop for classic Win32/WinUI3 apps; UWP unsupported.
- bgoffice: new hidden COM instances of Excel/Word/PowerPoint; invisible, no focus change measured. Outlook never touched.
- bgbench: 14 local GUI tasks. Vision-only 13/14, assisted 14/14; improved bgedge assisted 14/14, mean 61.6 s and 9.4 commands vs 67.4 s and 10.6.
- Skill `background-automation` routes: web -> bgedge, native -> bgdesk, Office -> bgoffice, computer-use last resort.

## 2026-09-30 00:00 JST - disk (2026-09-29/30)

- Cowork VM bundle (8.47 GB) deleted with owner approval; safe caches cleaned.
- miasma-wsl (42 GB WSL disk used by bench/swe_check.py) kept: no rebuild recipe found.

## 2026-09-30 00:00 JST - per-goal effort policy

- PR #68 (shadow: relay/effort_policy.py, telemetry mechanism effort_policy, replay tool) and PR #70 (fan-out children start one effort step below the parent, merge turn keeps the parent's level, sibling de-escalation; flag MCP_EFFORT_POLICY off|shadow|on, default off) merged. Design: `docs/private/20260930_effort_policy_design.md`.
- Measured before merging: effort mechanism rows were all config_source=run (nothing ever set a per-goal effort). Fan-out judgement rows are not fan-outs: real triggered fan-outs were 14 in total, recorded only from 09-26, 1,1,4,6 per day on 09-26..29 (goal mix changes daily; no causal claim).

## 2026-09-30 00:00 JST - root cause of core.bare flipping to true

- The pre-push hook exports GIT_DIR and a ratchet test ran `git init <tmp>`, re-initialising the shared git dir. Fixed by scrubbing GIT_* env in root conftest.py (test: tests/test_git_env_is_scrubbed_for_tests.py). Before the fix the live tree's shared config was reset to bare=false by hand twice.

## 2026-09-30 00:00 JST - origin/main merged into the live tree's branch

- Merge commit so the live fleet has the effort policy code (mode off). Push of that branch is blocked by the pre-push guard because of another engineer's untracked test file scripts/test_repair_children_are_windowless.py; not ours, not bypassed.

## Open items

- [x] effort_policy settings key + env-override surfacing (branch feat/effort-policy-setting-20260930, PR pending)
- [ ] GUI control in the cockpit (design doc pending; C# edit after codex's FleetCockpit changes are committed)
- [x] enable shadow via settings
- [ ] live in-run switching
- [ ] bench A/B: uniform min/auto/ultra vs policy on non-burned problems
- [ ] Copilot model/agent switching (unverified)
- [ ] Cowork / other disk items
- [ ] miasma-wsl decision
- [ ] end-to-end verification of the fixes on the fresh PC via quickstart.bat / start_all.bat only

## 2026-09-30 13:49 JST - effort setting merged, shadow enabled

- PR #71 merged (75c157d): settings key `effort_policy=off|shadow|on` (default off); mode() precedence is env MCP_EFFORT_POLICY > settings.txt > off; mode_info() returns (mode, source, conflict) and an env/settings conflict is logged once; telemetry rows carry extra.mode_source. GUI design: `docs/private/20260930_effort_policy_gui_design.md` (no C# edited yet: ui/FleetCockpit.cs is under active change by the other engineer; the control goes in a small PR after their changes are committed). PR #72 (the previous ledger entry) merged (cfcab4d).
- The live tree's branch received merges of origin/main twice (effort policy phases 1-2 with the git-env scrub; then the setting key), each after read-only checks (no overlap with uncommitted changes, merge-tree clean) and followed by checks (core.bare=false, git status works, import sanity, 87 effort/git-env tests passed). Push of that branch is still blocked by the pre-push guard because of another engineer's untracked test file scripts/test_repair_children_are_windowless.py (not ours, not bypassed).
- Shadow enabled: `effort_policy=shadow` was written directly into .config/settings.txt (backup kept in the temp dir) as a one-time exception on the owner's explicit instruction, because the GUI control does not exist yet. The cockpit's SaveKey rewrites line by line, so the unknown key survives (verified in code). Live check: mode_info() = ('shadow', 'settings', False). No effort_policy telemetry rows existed yet at 13:0x because the fleet run in progress had loaded the old code; rows are expected from the next fleet run (a watcher waits for the first row).
- Fan-out rate (measured from .fleet/mechanisms.jsonl, before shadow): real triggered fan-outs 1,1,4,6 on 09-26..09-29 (triggered was only recorded from 09-26); child judgement rows 9,5,31,62; top-level eligibility rate 56%,12%,76%,87% (goal mix changes daily; no causal claim). Correction of an earlier statement: 2,165 fanout rows were judgements, not fan-outs.
- Open items list ticked: settings key, enable shadow. Still open: first shadow row confirmation; cockpit C# control after the other engineer's FleetCockpit.cs commit; in-run switching (still shadow only); bench A/B; Copilot model/agent switching (unverified).

## 2026-09-30 15:28 JST - first effort_policy shadow row observed

- The first `effort_policy` telemetry row appeared in .fleet/mechanisms.jsonl for run r6abcab48_a0, worker w0, turn 0.
- It records the initial assignment as level `auto` with config_source run, and extra.mode_source = settings: settings.txt effort_policy=shadow is what took effect, so the GUI-key path works end to end.
- Only 1 row so far, because only one worker has started since the merge; in-run evaluation rows will accumulate with turns.
- Next: after a few days of data, run scripts/effort_policy_replay.py on the live ledger and decide whether to set effort_policy=on (initial assignment for fan-out children).
- Open items: first shadow row confirmation is done. Hidden-tool and disk items unchanged.

## 2026-09-30 - interrupted-run reap incident and resume design

- Incident: at 18:14:34 the fleet coordinator (pid 21520) crashed natively (sqlite3.dll 0xC0000006 on sessions.sqlite3-shm) while C: was full (0.2 GB free at 18:06). At 18:16:48 the supervisor's reaper marked every non-closed worker cancelled, including five healthy fan-out children; the fan-out parent had already ended done/FANOUT.
- Finding: the reaper's liveness check was correct. The misjudgement is what it writes: `cancelled` (terminal, reads as a user stop) instead of a resumable state, it also rewrites unclosed done workers, and it deletes fleet_run_active.json, the only input of the resume path, while auto-resume only runs at supervisor start.
- Design doc: docs/private/20260930_fleet_interrupted_resume_design.md (status `interrupted`, snapshot in .fleet/interrupted/, exactly-once merge via campaign id, free-space ring log, pre-resume crash gate, fan-out display state; no code changed).
- Owner's rule, restated: the disk floor (disk_floor_gb) is the owner's own setting. The assistant never changes it or its default; the design only reads and reports it and is independent of its value.

## 2026-09-30 - remote grading adapter and host probe

- Probed the remote grading host read-only over ssh: reachable non-interactively; memory and disk healthy (49.8 GB RAM free, 235.5 GB free on C:), but the grading WSL distro is Stopped and the dockerd task (SweDockerd) has been idle since 2026-09-15 with last result 1, so nothing can be graded until the owner starts it. Nothing on the host was started, changed or deleted.
- Added bench/remote_grade.py (one instance + one patch file -> structured result, infra vs graded-fail, injectable transport, CLI) and bench/test_remote_grade.py (20 tests, registered in ci.yml). bench/swe_check.py is untouched.
- Known-answer validation (gold resolves, empty patch rejected) NOT run: blocked by the stopped distro. Cached image list and swebench version are unknown for the same reason. grade.py is Lite-only, which limits the pilot to ids that are in Lite.
- Details and tags in docs/private/20260930_effort_bench_design.md, section "Remote grading: measured".

## 2026-09-30 - owner-facing prompt and family view design

- Design doc: docs/private/20260930_family_view_design.md (design only, no product code changed).
- Measured: over 313 sampled transcripts the first prompt contains the goal in all; of 1102 later user prompts 33% carry the whole goal, 44% only the 160-char `_task_anchor` line, 23% neither. 2096 of 2124 transcript files are gzipped and the cockpit has no gzip reader. FleetCockpit.cs never reads the `fanout`/`campaign_id`/`role`/`subtask_index` row fields. `_final_worker_entry` does not copy `subtask_index`. history.json rows carry no family keys.
- Decisions: refs and hashes only in status.json (first_prompt, latest_prompt, prompt_goal_intact, family_role, scope, family_members, family); pure module relay/family_view.py; UI in a WPF-free ui/FamilyView.cs extending the STAB-002 Content details, plus a Prompt tab. Overlap warning only for path-shaped scopes, labelled as claimed.
- Scanned for existing work (live tree, incl. untracked/ignored): relay/, ui/, tools/, docs/ (tracked and docs/private, docs/research read-only), .fleet small files and campaigns.jsonl/transcripts/status.json/history.json, reviews/. Only relay/fanout.py:754 family markers and relay/test_fanout_family_view.py existed.


## 2026-09-30 - common audit / lane policy / credential placeholder / exec pinning design

- Design doc only: docs/private/20260930_common_audit_and_policy_design.md (no product code changed). Ideas from a read-only study of a public sandbox runtime; nothing copied.
- Measured on this machine (100 runs each): tool_ledger append p50 11.2 ms / p95 13.9 ms, of which redact_secrets p50 8.7 ms (it re-reads .env and re-decrypts DPAPI per call); bare open-append-close p50 1.1 ms; msedge.exe (5.2 MB stub) SHA256 180 ms cold / 17 ms warm; Get-AuthenticodeSignature 221 ms in-process, 510 ms via a fresh powershell.
- Findings: .fleet/gate_audit.jsonl has no writer (last row 2026-08-31); decision ledgers other than tool_ledger write free text unredacted; sanitized_child_env already strips secrets from run_python/shell children; PyYAML is in requirements but tomllib is absent (Python 3.10.0); decision volume about 1,000 events/day.
- Friction budget: zero new prompts in any mode, <= 2 ms p50 added per audited action, 4 MiB fixed ring, enforcement off/shadow by default, promotion bar of at most 3 false would_deny per lane per day over 14 days. No disk-floor value read for or proposed by the design.
## 2026-09-30 - continuation prompts keep the whole goal; goal-fidelity analyzers

- Root cause from three trip-run transcripts: RelayWorker._task_anchor restated only the first 160 characters of the goal on every continuation turn, so a hard constraint after character 200 vanished from turn 3 on; one worker then told the reviewer the constraint was not in the original text (a false claim). An unlock/recovery payload with an empty goal section also replaced the whole task for several turns and, delivered as a follow-up, became a worker's goal (seeding a recycle prompt's goal).
- Fix on branch fix/continuation-keeps-the-goal-20260930: anchor restates the whole goal (6000-char cap, head and tail kept, cut marked), neutral wording unless the worker has checks, every continuation type anchored; recovery payloads get the goal back; recycle/replay raise EmptyGoalError instead of building with no goal; theme_from_goal no longer splits dates at the slash.
- Tests: relay/test_continuation_keeps_the_goal.py (28), relay/test_theme_from_goal_dates.py (4), scripts/test_goal_fidelity_report.py (13), all registered in ci.yml. Mutation check on a copy: 11 of 11 mutants killed. Full relay suite: 3982 passed; the 2 non-passing (test_repo_bug_fix_skill goal-builder test, test_gateway_executes) need a gitignored slice file / MCP_API_KEY absent from a fresh worktree.
- scripts/goal_fidelity_report.py: constraint-drift score and false-denial detector, read-only over transcripts. Baseline over r6abc7f6c_a0, r6abcd114_a0, r6abcdb87_a0 (26 transcripts, 83 assistant turns): drift 5/57 (0.088) with heuristic tokens, 8/57 (0.140) with explicit --must tokens; false denials 1, true 0.
- Design and planned before/after experiment: docs/private/20260930_goal_fidelity_design.md. Not merged into the live tree's branch.

## 2026-09-30 - reap marks an unfinished run interrupted, not cancelled

- Incident: coordinator pid 21520 died natively with the disk full; the supervisor reaper then rewrote every non-closed worker to `cancelled`, including healthy children, and deleted fleet_run_active.json (the only resume input).
- Fix (branch fix/reap-marks-interrupted-20260930, phase 1 of docs/private/20260930_fleet_interrupted_resume_design.md): relay/fleet_reaper.py marks only unfinished workers `interrupted` (outcome INTERRUPTED, pill 中断, color warn, reason "coordinator died: pid N is gone; last coordinator log write T", resumable, closed unchanged), leaves done/terminal workers byte-identical (done-but-not-closed included), `cancelled` only when an unconsumed `stop` command exists, done_count counts finished workers only. Snapshot `.fleet/interrupted/<run_id>.json` (marker copy, worker states, campaign plan from campaigns.jsonl, death evidence) is written atomically first; if that write fails nothing else is touched. Found and fixed on the way: the reaper's TERMINAL_STATUSES lacked `content_refused`.
- Vocabulary: relay/outcomes.py (STATUS_OF, NON_RETRYABLE, not FINISHED, SCORING fail, EXCLUDED_WITHOUT_WORK), relay_fleet.py label map (TERMINAL deliberately unchanged), fleet_runner.py pill map, task_router.py comment, ui/FleetCockpit.cs (IsInterruptedWorker, StatusLabel, auto-archive not blocked, counters/header, timeline, card tooltip), ui/Theme.cs (warning kind, ja/en labels). scripts/win/resume_interrupted_fleet.py reads the snapshot's marker copy.
- Tests: tests/test_reaper_marks_interrupted.py (17), tests/test_interrupted_status_is_classified_everywhere.py (6), retention pin in relay/test_fleet_retention.py, mirror test in tests/test_retry_sets_agree.py, outcome walker extended to `["outcome"] = ...` sidecar writes. Mutation checks on the worktree (5 of 5 killed): marking done workers, writing cancelled, retention including the dir, no snapshot, C# auto-archive block. Full cockpit compiled with csc in a temp dir (exit 0); rebuild_ui.ps1 not run. Deployment order: rebuild the cockpit before the supervisor pulls this reaper (an old cockpit paints an unknown status raw and stalls auto-archive).

## 2026-09-30 - continuation anchor is a compact ledger, not the whole goal

- The owner rejected PR #80's design of restating the whole goal (cap 6000 characters) in every continuation prompt: the context is small and long text every turn is wasteful.
- Replaced on branch fix/continuation-compact-ledger-20260930: the first message keeps the full goal; later prompts carry a deterministic ledger of at most 1000 characters (LEDGER_MAX_CHARS, relay/relay_fleet.py): task line, fixed-constraint sentences, fan-out scope block, pointer to the first message. Empty-goal guards, neutral wording and the theme_from_goal date fix are unchanged.
- Design and limits (extraction is a heuristic): docs/private/20260930_goal_fidelity_design.md.

## 2026-10-01 - split-group ledger view, Python side (PR 1 of the family-view design)

- New pure module relay/family_view.py (+ relay/test_family_view.py, registered in ci.yml): builds one compact summary per 分割グループ from status.json workers and campaigns.jsonl: group id, parent state, children by state (queued/running/done/failed/interrupted), merge state, and the constraint lines/tokens the goal ledger carries. The goal is shown only as the relay_fleet.goal_ledger output, clipped again (task 160, constraint 120 chars, max 5 lines), never the long text.
- `display_state` (awaiting_children = 待機中, ready_to_merge, merging, done, merge_failed, interrupted) is derived only; the parent's real status/outcome (FANOUT) and relay_fleet.TERMINAL are unchanged. JSON contract is in the module docstring for the C# side. No caller is wired yet and no C# changed.

## 2026-10-01 - phase 2 of interrupted-run resume (Python only)

- Branch feat/reap-phase2-resume-20261001 (worktree, not the live tree). Design: docs/private/20260930_fleet_interrupted_resume_design.md sections 2-4.
- G1 relay/fleet_runner.py: FANOUT is recorded in last_run_done.json (never over DONE); `_resume_goals` counts a FANOUT parent as done only when its campaign header is in campaigns.jsonl. G2 relay/fleet_resume.py `resume_children_goals` + fleet_runner main: unfinished children of campaigns without merge_done are re-queued; child ledger lines now carry `goal`; old lines degrade to text + header cwd, marked and logged. G3 relay_fleet.py: `merge_done` when the aggregator ends DONE, `child_result` lines (DONE slice answers, 1200 chars) so a resumed family can still merge, `merged` now carries `agg_key`, `merge_requeued` (cap 1); relay/fanout.py reader knows all four marker kinds (they would otherwise count as children).
- Loop guard: `resume_gate` (pure) in relay/fleet_resume.py, mirrored by Get-FleetResumeGate / Test-FleetShouldAutoResume -Gate in scripts/supervisor.ps1; parity test feeds one case table to both. The floor is only READ (settings_disk_floor); no value defined. Lineage: the resumer puts MCP_FLEET_RESUME_LINEAGE in the child env, the marker records it, the reaper's next snapshot inherits the resume block (otherwise a resumed run's new run_id would reset the count).
- Supervisor cycle call: after Invoke-FleetReap, DRY RUN unless -FleetCycleResumeLive; logs once per interrupted pid. Startup call also reads the pending snapshot.
- Free-space ring `.fleet/free_space_ring.jsonl` (32 KB fixed, 256 x 128 B, cap_jsonl only trims above 64 MB) and `.fleet/fault_p<pid>.log` (64 KB pre-zeroed, faulthandler); status.json `disk` block. Deferred: fleet_diag, cockpit dot, display_state, C#.

## 2026-10-01 family view production caller (PR #87)
- relay/fleet_runner.py `_snapshot` now calls `_attach_split_groups`: adds `groups` (family_view.build_groups, max 50) and a per-parent `display_state` key (annotate_display_state copies; status/outcome/pill untouched); any exception only omits the additions. Added `python -m relay.family_view [--fleet-dir DIR]` (main()). Fixes tools/test_nothing_new_is_built_without_a_caller.py without touching any allowlist.

## 2026-10-01 cockpit: split group line + interrupted filter (C# side)
- ui/FleetCockpit.cs only (no new .cs file, build list unchanged): fan-out parent chip shows display_label (kind by display_state), a one-line group summary (children counts, merge label, up to 4 token chips, tooltip = capped ledger only, never the goal) read from status.json groups; Interrupted filter tab (filter 4, shown only while one is on the board) with its own counter intN, not in badN. Phase-1 items already present were left alone. Tests appended to tests/test_interrupted_status_is_classified_everywhere.py. Compiled with bench/ui_build_check.build into a temp dir.

## 2026-10-01 effort policy: cockpit GUI + status.json fields
- Python: status.json top-level `effort_policy {mode, source, conflict}` (fleet_runner._snapshot via effort_policy.mode_info) and additive per-worker `effort_level/effort_source/effort_last_switch` (effort_policy.status_fields; EffortState exposed as worker.effort_state; shadow only, last switch marked record_only; absent when policy off). status/outcome/pill untouched. Note: shadow_tick reads worker.goal (text) not worker.goal_record, so an explicit goal effort never raises the virtual floor; status_fields uses goal_record. Not changed here.
- C#: new ui/EffortPolicy.cs (WPF-free, in rebuild_ui.ps1 Build line) + ComboBox off/shadow/on beside the effort selector, effort_policy= load/SaveKey, no-refire paint guard, each_gate timing entry, in-effect text with amber env-conflict warning, per-worker effort pill. Tests: ui/test_the_effort_policy_says_what_is_in_effect.py (executed csc harness + mutation + source asserts), relay/test_effort_policy_status.py; both in ci.yml.

## 2026-10-01 effort policy: shadow_tick reads goal_record
- relay/effort_policy.py: shadow_tick read worker.goal (TEXT) so an explicit goal effort never set the shadow initial level/floor. Added goal_dict(worker) (reads worker.goal_record) as the one accessor; status_fields and shadow_tick both use it, and shadow_tick seeds level/floor via initial_level. Sweep: no other .goal reader in effort_policy.py; relay_fleet hooks (shadow_tick, observe_child, worker_level, sibling_adjust, shadow_assign) take goal dicts or knobs. Tests: relay/test_effort_shadow_reads_goal_record.py (in ci.yml).

## 2026-10-01 first message not absorbed: verify and redeliver
- relay/first_reply_check.py (new, pure): first_reply_absorbed(reply, goal) -> (ok, reason). Short (<300) reply matching greeting / offer-help / ask-for-goal / empty-message / canned-refusal and no work evidence (verdict words, tool call, code fence, goal identifiers echoed) = not absorbed. Tuned on .fleet/transcripts (read-only).
- relay/relay_fleet.py (additive): worker keeps _first_message; _note_first_reply judges on arrival, _first_reply_gate (after infra handlers and policy-refusal, before goal_not_seen) redelivers the full first message with a one-line lead-in, max 2 per worker, then INFRA_STUCK with explicit reason; _task_anchor returns the first message instead of the ledger until absorbed (turn>=1). Mechanism first_message_not_absorbed in mechanisms.jsonl + transcript metric first_message_redelivery. Test: relay/test_first_reply_check.py (in ci.yml).

## 2026-10-01 tool-event measurement (timing + attribution), stage 1
- tools/tool_ledger.py: call rows gain `mono`, `proc`, `attr`; outcome rows gain `proc`, `mono`, `ts_start`, `ts_end`, `dur_mono_s` (existing fields unchanged). `task`/`worker` are now filled only from the worker's own turn-loop declaration (claim_turn/heartbeat/read_job_context job_id and worker_id) on the same MCP session, cleared at commit_turn/abort_turn; otherwise left empty.
- relay/relay_fleet.py: transcript metric `turn_wait_s` (send-to-reply, with t_send/t_done) derived from the existing send stamp; no frozen file touched.
- scripts/tool_event_report.py (read-only, markdown to stdout) with tools/test_tool_ledger_timing.py and scripts/test_tool_event_report.py, both registered in ci.yml. The ledger stays under the existing *.jsonl cap in relay/fleet_retention.py.

## 2026-10-01 resume scoped to the interrupted run
- Root cause: PR #86 G2 (fleet_runner --resume -> fleet_resume.resume_children_goals) re-queued the unfinished children of EVERY campaign in .fleet/campaigns.jsonl lacking merge_done (63 old campaigns -> 545 degraded goals for a 2-goal run), reset last_run_done.json to {} on resume, and each snapshot embedded every plan (80KB-2.6MB).
- Fix: fleet_resume.campaigns_of_run/interrupted_run_scope decide membership (new header stamp run_id+ts; else worker campaign id / parent-goal hash / lineage chain; nothing else, fail closed); snapshot embeds only scoped plans (cap 40x64, campaigns_scoped flag; unflagged old snapshots are not trusted); hard cap max(20, 4x goals) refuses with state pending (exit 6); resume no longer resets the done map. Sweep: relay_fleet._campaigns_from_disk left unscoped with reason in a comment; family_view/_campaign_already_on_disk are read-only/per-id.
- Tests: tests/test_resume_scoped_to_interrupted_run.py (in ci.yml); existing phase2 tests now pass scope explicitly.

## 2026-10-01 10:19 JST - phase2 audit follow-up / stabilization ledger checkpoint

Current working branch: `fix/phase2-audit-followups-20260929` (PR #78). The remote PR head is `183bb63`; all reported checks on that head are green: CI, CodeQL, Secret scan, Workflow lint, PowerShell lint, Windows build, and install-path E2E. The local branch had merged the then-stale local `origin/main` ref as `f2a3ef8`, which included PR #91's interrupted-run resume scoping. After this checkpoint was written, an explicit `git fetch origin main` showed that remote `main` had already advanced further to `ed16ac9` via PR #92. Therefore `f2a3ef8` is not the current-main convergence point; the branch still needs the newer `ed16ac9` main before final integration.

Stabilization work now present on this branch, in addition to the items already recorded above:
- live Fleet submission path is repaired and accepted through the GUI AutomationId path; socket capture was moved off the Fleet sweep so expensive Playwright/CDP capture no longer blocks command draining;
- silent socket turns recover after the measured 60 s no-progress boundary instead of remaining in the old long-wait path;
- GUI submissions are serialized by the dedicated GUI submit lock, preventing foreground-window races and overlapping automated submitters;
- legacy `.env` secrets are migrated during atomic edits while preserving quoted values;
- planned server-restart health state is bound to the supervisor instance and its yellow-path poll is exercised exactly in test;
- explicit replies that say unlock is not required are no longer misclassified as locked, while literal lock evidence and genuine unlock failure remain authoritative;
- PR #91, now on main, scopes interrupted-run resume to campaigns belonging to the interrupted run, preventing old unfinished campaigns from being requeued into a new resume.

Current validation state:
- PR #78 remote head `183bb63`: all GitHub checks green;
- main stabilization ledger records all previously enumerated P0 behavior regressions CLOSED with live/exact evidence;
- local worktree has no tracked modifications at this checkpoint; only unrelated untracked directories `kanazawa-trip/`, `output_20260930/`, and `reviews/` exist and are intentionally untouched.

Next work after this checkpoint:
- PR #78 was pushed through this ledger checkpoint; before final integration, merge/rebase the newer fetched main `ed16ac9` (PR #92) and re-run the full blocking checks;
- continue the phase2 audit follow-ups only after confirming the post-merge CI/CodeQL/Windows path remains green;
- separately inspect current `main` CI failures if any remain; do not mix unrelated main-CI repairs into the Fleet stabilization commit unless the cause is shared.

### 2026-10-01 10:2x JST correction -- fetched main was newer than cached origin/main

After the checkpoint above, `git fetch origin main` updated the remote-tracking ref from PR #91's `3d97c39` to `ed16ac9` (PR #92). Main HEAD `ed16ac9` is green on CI, CodeQL, Secret scan, Workflow lint, and Windows build. Treat the earlier wording that called `f2a3ef8` a merge of current main as stale-local-ref wording; final PR #78 convergence still requires the newer main.

### 2026-10-01 -- tool-call attribution moved to the coordinator (branch fix/tool-event-attribution-from-coordinator-20261001)

Root cause read from the code: PR #92 attributed a tool call only when the worker's own MCP session called claim_turn / heartbeat / read_job_context with job_id; real Copilot workers never do (about 1 claim_turn in 5,718 calls, fill 0.0% of 37,156 rows). Fix: relay_fleet writes `.fleet/turn_context.jsonl` (open row at the send, close row at the reply; worker, job, run, turn, t_send, t_done, wall ts, mono, proc, pid; writer rotates at 8 MB, retention cap_jsonl also covers it). tools/tool_ledger.py labels an unlabelled call from those windows by wall-clock epoch time with 1.5 s slack at each edge: one worker in flight -> attr=window; several but the MCP session was bound earlier by an unambiguous match -> session-window; several and no binding -> task/worker left empty, attr=ambiguous. Explicit and turn-loop-declared identities still win. scripts/tool_event_report.py now reports fill by attr kind. The MCP server and the coordinator must both be restarted to pick this up. Large fleets will stay mostly ambiguous (see relay/turn_windows.py measurement); that is reported, not guessed.

## 2026-10-01 - correction: what the recovery work does and does not cover

Earlier entries and chat reports called the interrupted-run recovery work (non-destructive reap, exactly-once resume) a finished "phase" or "stage done". That wording was wrong in two ways. It is one item (duplicate suppression across retry/resume) of a larger recursive fan-out effort that is not built: there is no task-node identity and there are no grandchildren. And "done" meant "code merged", not "exit criteria met".

Status in plain terms:
- Done and verified live for 2 goals: non-destructive reap, interrupted snapshot, exactly-once merge on resume.
- Still open: resume of the whole fan-out family, supervisor auto-resume (currently off), stage-2 outcome comparison, the stage-1 on-screen check, and tool-event attribution.
- Existing entries above are left as written; read "stage done" there as "code merged".

## 2026-10-01 15:00 JST - one STUCK worker no longer becomes several copies
- Cause (2026-10-01, one job ran 3 concurrent copies): the ambiguous-fresh-submit end was STUCK with prose "not retried" but no `retryable_override`, so the runner and the cockpit (both keyed on the outcome string) re-queued it; the cockpit also re-queued the same terminal worker every tick (budget keyed by goal text only).
- Fix (branch fix/stuck-worker-retried-once-20261001): (1) ambiguous submit sets retryable_override=False, status rows export `retryable` and `retry_queued`, cockpit IsRetryableWorker honours `retryable:false`; (2) cockpit remembers re-queued workers (jid or name+run_id), skips ones the runner already re-queued or whose goal is live; (3) runner add_goal refuses a retry-tagged goal already queued/running (mechanism row); (4) a recycle that overflows on its first reply twice in a row ends non-retryable instead of repeating up to 8 times (conservative variant: tool-call identity is not visible to the worker, so it keys on first-turn overflow).
- Tests: tests/test_stuck_worker_retried_once.py (in ci.yml). Open: PR review/merge.

## 2026-10-01 - fan-out default ON in every layer, visible GUI box, CLI-only audit
- Finding: the default was already ON in the cockpit field, the runner flag and `_wants_fanout`; the registry said "undecided". No product path seeds `fanout=off`. The real defect: autostart passed `--fanout` for yes and NOTHING for no, so the runner's default (ON) overrode an explicit off.
- Change (branch fix/fanout-default-on-gui-only-20261001): registry default True (UNDECIDED removed); autostart names `--fanout` / `--no-fanout`; runner writes `fanout_run` {enabled, source} into status.json; cockpit gets a visible On/Off box (FanoutView in ui/EffortPolicy.cs, no build-list change) with "in effect" and "applies from next start" text.
- Existing explicit `fanout=off` lines stay honoured; no migration (a frozen default cannot be told from a choice). Owner changes it in the new box.
- Tests: tests/test_fanout_default_on_agrees.py (in ci.yml). Audit: docs/private/20261001_cli_only_settings_audit.md. Open: PR review/merge.