# 2026-09-26 Fleet/CopilotAgent usage notes

## status.json is not reliably consumable with Windows PowerShell 5.1 ConvertFrom-Json

While preparing a GUI-only CopilotAgent dispatch, FleetCockpit and `goalInput` were detected correctly by `scripts/win/submit_via_ui.ps1 -ReadOnly`, but this command failed when manually parsing `.fleet/status.json` with:

```powershell
Get-Content .fleet\status.json -Raw | ConvertFrom-Json
```

The file clearly contained a live run (`running=true`, worker phase `refuting`), but PowerShell emitted a JSON parse error while also printing mojibake around Japanese text. This makes ad-hoc PowerShell status inspection unreliable and risks an operator incorrectly concluding that no run is active.

Operational consequence: do not start an unrelated fleet solely because a PowerShell `ConvertFrom-Json` read failed. Prefer the repository's own UTF-8/UTF-8-SIG-aware readers/checkpoint/status tooling.

## GUI-only dispatch correctly blocked by an existing unrelated run

At 2026-09-26 19:46 JST an unrelated READ-ONLY audit was still active. Per GUI-route semantics, sending a new goal while that run is active would either be rejected or become a steer and contaminate the existing task. The new ShuttleScope audit was therefore intentionally not dispatched yet.


## CopilotAgent repository-search tooling degraded to catalog fallback

During the ShuttleScope READ-ONLY audit, the CopilotAgent report recorded that its `find_files` / grep-style calls using `name_contains` or `pattern` did not resolve and fell back to a tool catalog. It completed the audit by using `read_file` + `list_directory` instead.

Operational consequence: broad repository audits become slower and may miss evidence if the agent assumes grep/find succeeded. Until fixed, audit prompts should require exact file/function references and the supervising layer should independently verify important claims.

## Report written while Fleet run still reported active

The audit report `private_docs/copilotagent_remaining_audit_20260926.md` existed with a substantive completed-looking report while `.fleet/status.json` still showed `running=true` with w0=waiting and w1=pending. This may be normal multi-worker synthesis behavior, but it makes "report file exists" an unsafe completion signal.

Operational consequence: do not treat output-file existence as task completion. Require terminal Fleet state / closed workers before using a delegated report as final evidence.

## Fleet reliability defects found during the 2026-09-26 stabilization

The live Fleet path had several independent durability / ownership gaps. They compounded, so a symptom such as "I added a task and it did not appear" could come from more than one cause.

1. Live `add_goal` items were not appended to `last_run_goals.json`. A crash/reboot could therefore resume launch-time goals while silently forgetting work accepted later.
2. Finalization rebuilt `last_run_done.json` from only the last reconnect/stop chunk. A graceful stop could erase DONE outcomes from earlier chunks and make `--resume` replay completed work.
3. `fleet_run_active.json` had no ownership check on deletion. An old coordinator finishing cleanup could delete a newer coordinator's marker.
4. `fleet_runner` itself had no state-dir single-instance exclusion. Outer launcher guards existed, but three coordinators were observed concurrently writing the same `.fleet` state. A state-dir OS byte-range lock plus live-marker guard is now the final exclusion layer.
5. `read_commands()` deleted a command before `_apply_command` durably applied it. A crash between delete and live-ledger append could destroy a task after the UI had reported it queued. Production draining now uses atomic claim -> apply -> commit; dead-PID claims are recovered.
6. The Cockpit live composer could write an `add_goal` after the runner's final command sweep but just before the run ended. The file survived but nobody consumed it. Live composer writes are now tracked with an ack; if the run ends without an applied receipt, the Cockpit launches `fleet_runner --adopt-command <exact pending file>`. Adoption happens only after winning the state-dir OS lock, so it cannot race a newly-started coordinator into duplicate execution.

Validation performed: 201 related pytest cases passed; FleetCockpit and CopilotChat rebuilt with csc; isolated real-browser E2E completed `commands.d -> --adopt-command -> applied ack -> worker DONE -> done map -> active marker cleanup`, leaving zero pending command JSON files.

## Displayed goal text was unusably long -- fixed after reliability stabilization

The execution goal still keeps the full instruction text, but the Cockpit no longer uses that 2-4k-character operational prompt as its primary visible identity. The measured archive contained recent goals of 1,900-3,500+ characters, and the Directive band concatenated every full goal, making it hard to see what each lane was actually doing.

The runner now emits a separate `goal_summary` per worker plus `directive_summary` / compact `run_label`. These reuse the existing deterministic `relay.conv_title` extractor: extractive only (no model call / no invented outcome), bounded, and display-redacted. Policy-only openings such as `READ-ONLY audit only; do not edit...` are skipped in favor of the next task-identifying sentence. The authoritative `goal` / `directive` remain byte-for-byte full text for execution, retry, resume, search, and the expanded Overview. Both history archive routes persist `goal_summary`, while old rows fall back to the previous CardTitle logic.

Validation: 311 related pytest cases passed, the legacy fleet_runner fix harness passed 61/61, and both WPF binaries rebuilt/restarted successfully.
