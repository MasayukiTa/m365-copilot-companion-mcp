# Watchdog kill -> relaunch evidence

This directory holds a REPRODUCIBLE, SAFE demonstration that
`scripts/supervisor_watchdog.ps1` behaves as designed on a real Windows host:

- while a supervisor process is alive -> the watchdog **waits** (does nothing);
- once the supervisor process is gone **and** its heartbeat is stale -> the watchdog
  **relaunches** `start_all.ps1 -CoreOnly -NoSplash`.

## Why this exists

Measured 2026-11-11/12: `scripts/supervisor.ps1` (pid 25644) ended 23:58:02 with
`0xC00000FD` (native stack overflow); nothing restarted it for ~37 minutes and the
public URL answered 502 until a person ran start_all by hand. supervisor.ps1 cannot
restart ITSELF after a hard kill. The out-of-process watchdog does. The unit tests
(`scripts/supervisor_watchdog.Tests.ps1`, `scripts/test_supervisor_watchdog_gate.py`)
pin the pure decision table; THIS evidence shows the end-to-end kill -> relaunch on a
real process, with timestamps.

## Safety

The demo never touches the live supervisor, the live heartbeat, the real start_all,
or any devtunnel. It starts a throwaway PowerShell process as a stand-in supervisor,
writes a heartbeat into an isolated temp sandbox, and points the watchdog at that
sandbox via its `-HeartbeatPath` / `-StartAll` parameters. The stand-in start_all
only stamps a proof file. It kills only the throwaway stand-in it started itself.

## Run it

```
powershell -NoProfile -ExecutionPolicy Bypass -File docs/evidence/demo_watchdog_kill_relaunch.ps1
```

## Recorded run

See `watchdog_kill_relaunch_run.log` for a captured run. Key lines (UTC timestamps):

- PHASE2 ALIVE  -> `decision=wait`   (supervisor pid alive, fresh heartbeat)
- PHASE3        -> stand-in supervisor killed
- PHASE5 GONE+STALE -> `decision=relaunch`, `relaunched=true`
- PHASE5 proof  -> `RELAUNCHED start_all CoreOnly=True NoSplash=True at ... pid=...`
