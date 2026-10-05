## 2026-10-05 - The supervisor knows when it runs older code, and can replace itself

- Problem (found live). `scripts/supervisor.ps1` is a PowerShell script: it is parsed ONCE, when its
  process starts. The supervisor started 2026-10-04 20:04 kept running its pre-#122 text after the
  auto-resume change (#122) was merged and the working tree updated, so when the coordinator died it
  logged "fleet auto-resume DRY RUN -- not relaunching (verification mode)" and nothing resumed.
  The server, bridge and coordinator pick up code on their own restarts and the cockpit is rebuilt by
  `ui/rebuild_ui.ps1`; the supervisor had no staleness handling at all.

- Investigation (read-only): how the supervisor is started, supervised, restarted.
  - Started by `scripts/start_all.ps1` (`Start-FreshSupervisor`: hidden `powershell -NoProfile
    -ExecutionPolicy Bypass -File scripts\supervisor.ps1 -TunnelName "<tunnel>"`, stderr to
    `supervisor.err.log`, 3 s survive check). start_all is reached from `start_all.bat`, from the
    logon autostart (Startup-folder shortcut made by `scripts/register-supervisor.ps1`, target
    `start_background_hidden.vbs` or `start_all.ps1 -NoUi -NoSplash`), from `quickstart.bat`, and from
    the cockpit's health auto-heal / Fix button (`RunStartAll`).
  - Single instance: `New-Object Threading.Mutex($true, "Global\m365-copilot-companion-supervisor")`
    near the top of supervisor.ps1; a second copy logs "another supervisor already running" and exits.
    start_all additionally has its own machine-wide guard so the double logon launch cannot race.
  - What start_all does with a running supervisor: nothing, EXCEPT when its `-TunnelName` differs from
    `.env` (`Test-SupervisorTunnelDrift`): it stops the supervisor (`Stop-Process`) and starts a fresh one.
    Dependency installs (`Invoke-DependencySync`) also stop and restart it, but only when
    `stale_server_check.py --server-action` says nothing is live. There was NO staleness check.
  - On kill: NOTHING relaunches a killed supervisor. `start_bridge.ps1 -Keepalive` supervises only the
    bridge (its own mutex `Local\M365CopilotCompanion_BridgeKeepalive_<port>_<cdp>`). The supervisor comes
    back only through start_all (logon, manual, cockpit auto-heal when Server/Tunnel are red).
  - The two long-lived `powershell.exe -File` processes in the tree on 2026-10-05:
    pid 26560 = `scripts\supervisor.ps1 -TunnelName "<tunnel>"` (started 2026-10-04 20:04), and
    pid 31136 = `scripts\start_bridge.ps1 -Keepalive` (the bridge keepalive, started 2026-10-05 03:57).
    A third (`start-c2c-relay.ps1`, another project) is not part of this product.
  - The supervisor's children (`main.py` server, `devtunnel host`) are separate processes: replacing
    the supervisor does not stop them, and a fresh supervisor adopts them (it launches the server only
    when nothing listens, and hosts the tunnel only when nothing of ours hosts it).

- Change.
  - `scripts/supervisor.ps1`: at the top of the script it records sha256 of itself and the script it
    dot-sources (`scripts/tunnel_name_util.ps1`) plus git HEAD read from the `.git` files (no git
    process), and writes `.fleet/supervisor_state.json` (`pid`, `start_ts`, `fingerprint`, `git_head`,
    `supervisor:{pid, started, stale, changed_files}`, `self_restart:{verdict, ts}`). Each tick,
    `Invoke-SupervisorCodeCycle` stats the files (mtime + length) and hashes only a file whose stat
    moved; a difference is logged ONCE and exported.
  - Self-restart, setting `supervisor_self_restart` (off|on, default ON, registry `EACH_GATE`, read by
    `relay/code_staleness.py:self_restart_setting` only once the code is stale). Default on because
    the failure it removes already happened and every gate below must hold. It restarts only when
    `Get-SupervisorRestartVerdict` says ok: setting on; the new scripts parse (in-process
    `Parser.ParseFile`, same parser PowerShell loads with); the previous self-restart is more than 10
    min old (`.fleet/supervisor_selfrestart.json`, written BEFORE the handoff so a bad restart cannot
    loop); no coordinator of this checkout; no pending interrupted snapshot (the supervisor's own
    resume must run first); no resume in progress (tracked runner or fresh `resume_launch.json`); no
    review / LOCAL_LOOP run alive; bridge `/status` idle (unreadable counts as busy unless the port
    proves no bridge of ours). Reasons are exported as `self_restart.verdict`.
  - Relaunch path (did not exist; designed minimally): `scripts/supervisor_handoff.ps1` (ASCII). The
    old supervisor starts it with its own arguments, confirms it is alive for 2 s, then exits (so the
    mutex is released). The helper waits for the old pid (90 s; if it never exits, nothing is started),
    starts the new supervisor, requires it to survive 6 s, retries up to 5 times 20 s apart, and logs to
    the supervisor log; after 5 failures it logs "NO SUPERVISOR IS RUNNING ... run start_all". If the
    helper cannot be started the old supervisor stays up.
  - Cockpit: gear popup, Recovery section, new control "Supervisor self-restart" (SaveKey only) and,
    when the supervisor reports stale, the plain message "supervisor is running older code: restart
    needed" with the changed files and the verdict (waiting for idle / off / parse error / ...).
    `SupervisorCodeView` in `ui/EffortPolicy.cs` (WPF-free). Nothing added to the header. The file is
    ignored when its pid is dead or was born at another time.
  - Bridge: `bridge/copilot_bridge.py` fingerprints the files it loaded at start (`relay/code_staleness.py`
    `CodeWatch`, stat first) and `/status` gains `code_stale` (open route) and `code_changed` (token
    holders only). Additive.

- Tests. `scripts/test_supervisor_stale_code.py` (real PowerShell drivers over extracted functions: not
  stale, changed => stale + exported + logged once, each refusal, handoff + exit + mark, handoff
  failure keeps the old supervisor, loop guard edges, the helper against a fake supervisor: same
  arguments, retries, never beside a live one), `relay/test_code_staleness.py`, the settings-registry
  test (`tools/test_a_setting_declares_when_it_takes_effect.py`).

- Other long-running scripts, same property (loaded once; recommended handling).
  - `scripts/start_bridge.ps1 -Keepalive` (a PowerShell loop, pid 31136): same staleness as the
    supervisor, plus the bridge python it restarts picks up `bridge/*.py` only when it restarts it.
    Detection for the bridge process is now `/status code_stale`; the keepalive PS script itself is NOT
    fingerprinted here. Recommended: the same record-and-compare in start_bridge.ps1, restart gated on
    `turn_running`/`busy` false (the existing bridge-restart rule in `scripts/test_a_bridge_restart_respects_a_live_turn.py`).
  - `bridge/copilot_bridge.py` (python, loaded once): now reports `code_stale`; its restart must stay
    idle-gated (a bridge killed mid-turn loses the answer). Not auto-restarted by this change.
  - `main.py` server: already handled (`server_code` in `/health`, `Invoke-StaleServerCycle`).
  - Coordinator (`relay.fleet_runner`, per run): picks up code on its next launch; a running one keeps
    its imports by design, never restarted under a run.
  - Cockpit exe: rebuilt by `ui/rebuild_ui.ps1`; the running exe is the old build until restarted.
  - `devtunnel host`: not our code.

- UPDATE 2026-10-05 20:44: the live supervisor that predated this change (the old pid) was already
  replaced by hand and is up to date, so the procedure below is no longer needed for it. It is kept as the
  manual path for any supervisor that predates this change (such a supervisor cannot restart itself
  because it does not contain this code).
- SAFE RESTART PROCEDURE for a supervisor that predates this change. Do it AFTER the merge is pulled into
  the live checkout (`<repo>` = the live checkout root).
  1. Preconditions, all read-only, in this order; any "no" means wait:
     a. No coordinator: no process whose command line matches `relay[\\/.]fleet_runner` and `<repo>`.
     b. No resume in flight: `.fleet\resume_launch.json` absent or older than 10 minutes.
        (A pending `.fleet\interrupted\*.json` is FINE and wanted: the new supervisor resumes it at
        startup, behind the loop guard.)
     c. No review run (`.fleet\review_run_active.json` with a live pid) and no LOCAL_LOOP marker alive.
     d. Bridge idle: GET `http://127.0.0.1:<bridge port from .env>/status` shows `turn_running` false and
        `busy` false.
     e. Parse check: `[System.Management.Automation.Language.Parser]::ParseFile('<repo>\scripts\supervisor.ps1',
        [ref]$null,[ref]$e)` leaves `$e` empty (also `supervisor_handoff.ps1`).
  2. Start the helper FIRST (it waits for the old pid, then starts the new supervisor and retries):
     `Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','"<repo>\scripts\supervisor_handoff.ps1"','-OldPid','26560','-TunnelName','"<tunnel name from the old command line>"'`
  3. Then stop ONLY the old supervisor, after confirming its command line names `<repo>\scripts\supervisor.ps1`:
     `Stop-Process -Id <old pid>`. Do not touch the server, devtunnel host, bridge or keepalive.
  4. Verify within about a minute: the log (`%TEMP%\m365-companion-supervisor.log`) has "[handoff] new
     supervisor started" and a new "supervisor up" line; `.fleet\supervisor_state.json` has a pid other
     than 26560 and `supervisor.stale` false; the old pid is gone; `/health` still reports the same
     `server_pid`; the tunnel host count did not drop. If the log says "NO SUPERVISOR IS RUNNING", run
     `scripts\start_all.ps1 -NoUi -NoSplash` (it starts one).
  Without the helper (fallback): stop 26560, then `Start-Process powershell -WindowStyle Hidden
  -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','"<repo>\scripts\supervisor.ps1"','-TunnelName','"<tunnel>"'`.
  Every supervisor from this change on does steps 1-4 itself.

- Open. Fingerprinting `start_bridge.ps1` and idle-gated auto-restart of the bridge keepalive; showing the
  stale note in the header was deliberately NOT done (header controls are pinned by a test). The
  fingerprint covers the supervisor script and what it dot-sources; python modules the supervisor runs
  via `python -c` are fresh per call and need no handling.
