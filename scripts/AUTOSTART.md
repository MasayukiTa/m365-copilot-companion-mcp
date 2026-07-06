# M365 Companion Autostart

## What This Does

The autostart task automatically launches the M365 Companion stack (`start_all_hidden.vbs`) at every logon.
The full application stack — server, tunnel, bridge, and all UIs — comes up automatically in the background
without any manual intervention.

## Requirements

- **Per-user** (non-admin): no elevated privileges required
- **Windows Task Scheduler**: automatically runs at interactive logon
- **One-time setup**: register the task once; it persists across reboots

## Setup

### Register the Autostart Task

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-supervisor.ps1
```

On success you will see:
```
Registered M365CompanionAutostart (per-user, AtLogOn, non-elevated).
Start now without logout:  schtasks /Run /TN M365CompanionAutostart
Remove:  scripts\unregister-supervisor.ps1
```

### Verify Registration

```cmd
schtasks /Query /TN M365CompanionAutostart
```

Expected output includes:
- `TaskName`: M365CompanionAutostart
- `Schedule Type`: At logon
- `Run As User`: your domain\username
- `Run Level`: Limited (non-elevated)

### Start Immediately (Without Waiting for Logon)

```cmd
schtasks /Run /TN M365CompanionAutostart
```

## Removal

To unregister the autostart task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\unregister-supervisor.ps1
```

You will see:
```
Removed M365CompanionAutostart
```

## Self-Healing Behavior

The autostart task is **idempotent** and **self-healing**:
- If a process dies, `supervisor.ps1` (launched by `start_all_hidden.vbs`) automatically restarts it
- If you reboot, the task re-launches the entire stack automatically
- Most failures are automatically recovered

**The only manual action still required:** genuine M365 sign-in / certificate prompts when your credentials or certificate change.
Everything else self-heals via `supervisor.ps1`.
