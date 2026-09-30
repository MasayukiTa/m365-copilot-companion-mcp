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
