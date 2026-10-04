## 2026-10-05 - Auto-resume of an interrupted run is a setting, and it goes before the queue

- Problem (the original incident, again, found live). A coordinator died, the reaper wrote
  `.fleet/interrupted/<run>.json` (state=pending) and nothing resumed it. Two causes:
  (a) the supervisor's per-cycle resume was a DRY RUN unless `-FleetCycleResumeLive` was on the
  command line, a switch with no GUI control, so for the owner the feature did not exist;
  (b) with goals queued, the router saw "no fleet is live" and autostarted a FRESH coordinator
  that ignored the snapshot, so the interrupted trees were lost.
- Change. New setting `fleet_auto_resume` (off|on), registered in `tools/settings_keys.py`
  (EACH_GATE: re-read every supervisor cycle), read through `relay/fleet_resume.py:
  auto_resume_setting` / `auto_resume_enabled`. `MCP_FLEET_AUTORESUME`, when set, still wins;
  `-FleetCycleResumeLive` now only forces on over a setting of off (override, no longer needed).
  `scripts/supervisor.ps1`: the cycle call is live when the setting is on; the setting is asked
  only once a resume candidate exists (no python spawn on an ordinary tick).
- DEFAULT = ON. Decision and evidence. The owner's incident requires an interrupted run to come
  back by itself. The unattended path is already bounded by `resume_gate`: at most 3 automatic
  resumes per run id (the count is inherited through `resume.lineage`, so a run that dies again
  does not reset it), backoff 5 min x 2^count, never when `stop_requested`, never when a
  coordinator of this checkout is alive, never under the operator's disk floor (read from the
  existing accessor), and not again after the same crash signature with no more free space; the
  PowerShell gate is pinned to the Python gate by a table-driven parity test. The startup resume
  was already ON by default, so ON is also the consistent value. Verified here with fakes, not on
  the live fleet: on => launched once; off => never; env override both ways; loop cap, stop,
  backoff and "coordinator already live" all refuse.
- Ordering. `relay/task_router.py:autostart_status` now asks `fleet_resume.autostart_hold`
  before it may start a fresh coordinator. It holds while (1) a resume launch guard is fresh
  (`.fleet/resume_launch.json`, written by the supervisor BEFORE `Start-Process` with pid 0 and
  completed with the pid, dropped if the launch fails; bounded by 10 min and by the pid being
  alive) or (2) auto-resume is on and a pending snapshot's gate verdict is ok / backoff /
  below_floor and the snapshot is younger than 1 h. Final refusals (loop cap, stop requested,
  same crash, state gave_up/resumed) never hold, so a queued goal cannot be stranded. The goal
  is parked in `for_fleet/` and joins the resumed run through the ordinary live-fleet delivery.
  The supervisor already ran resume before the queue drain in a cycle; the guard closes the gap
  to the next router pass (and to the server handing over a goal directly).
- GUI. Cockpit gear popup, new section `Recovery / 復旧`: on/off selection
  "Auto-resume interrupted runs / 中断した実行を自動で再開" (SaveKey only, no-refire paint guard,
  junk value keeps the value). The "in effect" line comes from an additive `status.json` field
  `auto_resume {setting, last_decision, pending_snapshots}` written by
  `fleet_runner._snapshot`; `record_resume` / `record_blocked` also persist the last decision
  (`.fleet/auto_resume_state.json`) and patch an IDLE status.json so the screen answers while no
  coordinator is alive. Words live in `AutoResumeView` (ui/EffortPolicy.cs, WPF-free). No header
  control was added.
- Tests. `relay/test_fleet_auto_resume_setting.py` (setting, env, ordering through
  `fleet_handoff`, hold table, decisions, status block, cockpit source shape),
  `scripts/test_supervisor_fleet_resume_guard.py` (real PowerShell functions against a temp
  checkout with the real `fleet_resume.py`), `tools/test_a_setting_declares_when_it_takes_effect.py`
  (new reader). `bench/ui_build_check` builds both exes clean.
- Risk for unattended resume. A resumed run spends Copilot budget without anyone watching; the
  cap (3) and the backoff bound that, and the switch is one control away. The hold can delay a
  queued goal by at most 1 h if the supervisor is not running. Not exercised on the live fleet.
- Open. `resume_interrupted_fleet.py` (start_all) does not write the launch guard; its own
  coordinator-live check covers the same window there.
