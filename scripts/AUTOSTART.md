# M365 Companion Autostart

## What This Does

The autostart mechanism automatically launches the M365 Companion background stack
(`start_background_hidden.vbs`) at every logon. The server, tunnel, companion Edge, and bridge come up
without any visible splash or WPF UI windows. Manual launchers can still use `start_all_hidden.vbs` to open
the full application stack including the normal update check plus the chat and cockpit windows.

## Requirements

- **Per-user** (non-admin): no elevated privileges required, no Windows Task Scheduler required
- **Startup folder**: a shortcut in `shell:startup` runs automatically at every interactive logon
- **One-time setup**: install the shortcut once; it persists across reboots

## How It Works

The primary mechanism is a shortcut placed in the per-user Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`). Windows launches every shortcut in that
folder automatically at logon, with no admin rights and no Task Scheduler involved. This folder is
writable even on locked-down corporate PCs where Task Scheduler registration is blocked by policy.

Task Scheduler registration is still attempted afterwards as an **optional bonus** (best-effort, wrapped
so it can never fail the setup). On corporate PCs where policy blocks it, you'll see a message saying so
and setup still succeeds — the Startup-folder shortcut is the mechanism that actually matters.

## Setup

### Register the Autostart Shortcut

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-supervisor.ps1
```

On success you will see:
```
Installed autostart shortcut: C:\Users\<you>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\M365 Companion.lnk
It launches at every logon (per-user, no admin).
Start the background stack now:  wscript.exe "<repo>\scripts\start_background_hidden.vbs"
Start the full UI stack now:     wscript.exe "<repo>\scripts\start_all_hidden.vbs"
Remove:  scripts\unregister-supervisor.ps1
```

On a locked-down corporate PC you will additionally see:
```
Task Scheduler unavailable (corporate policy); Startup-folder shortcut is the active mechanism.
```
This is expected and does not indicate failure — the script still exits 0 and the Startup shortcut is installed.

### Verify Registration

Check that `M365 Companion.lnk` exists in the Startup folder (open `shell:startup` in Explorer, or):

```powershell
Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'M365 Companion.lnk')
```

### Start Immediately (Without Waiting for Logon)

```cmd
wscript.exe "scripts\start_background_hidden.vbs"
```

To start the full UI stack manually:

```cmd
wscript.exe "scripts\start_all_hidden.vbs"
```

## Removal

To remove the autostart shortcut (and the bonus Task Scheduler task, if it was registered):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\unregister-supervisor.ps1
```

## Self-Healing Behavior

The autostart mechanism is **idempotent** and **self-healing**:
- If a backend process dies, `supervisor.ps1` automatically restarts it
- If you reboot, the Startup shortcut re-launches the entire stack automatically
- Most failures are automatically recovered

**The only manual action still required:** genuine M365 sign-in / certificate prompts when your credentials or certificate change.
Everything else self-heals via `supervisor.ps1`.
