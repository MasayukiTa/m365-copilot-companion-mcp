# start_companion_edge.ps1
# Launch a DEDICATED, ISOLATED Edge instance for the companion (bridge + fleet).
#
# Why a separate instance (not your everyday Edge):
#   1. --remote-debugging-port only binds on a FRESH Edge instance. A distinct
#      --user-data-dir guarantees a fresh instance, so the CDP port reliably opens.
#   2. It is fully isolated from your personal Edge. Your main browser can hold
#      dozens of heavy M365 tabs; they cannot starve the companion's RAM or take it
#      down, and the companion closing its tabs frees only its own memory. This is the
#      fix for the "many M365 tabs -> Edge crashes -> port 9222 disappears" failure.
#
# One-time: a window opens on M365 Copilot. Sign in with your work account once; the
# profile persists, so future launches are already signed in. The bridge and the
# parallel fleet both connect to http://127.0.0.1:<port>.
#
# Usage:
#   .\start_companion_edge.ps1                 # default port 9222
#   .\start_companion_edge.ps1 -Port 9333      # custom port

param(
    [int]$Port = $(if ($env:MCP_CDP_PORT) { [int]$env:MCP_CDP_PORT } else { 9222 }),
    [string]$Url = "https://m365.cloud.microsoft/chat",
    # Recovery: kill the companion Edge and WIPE its session-restore state before
    # launching, so wedged tabs are NOT restored. Use this when the dedicated Edge has
    # stopped responding (CDP dead) and the fleet's attach() stalls. For a still-
    # responsive Edge, prefer closing tabs one by one: python -m relay.edge_recover
    [switch]$HardReset,
    # Keep the window visible (this is the DEFAULT now -- it is the stable mode).
    [switch]$Foreground,
    # EXPERIMENTAL: minimize + keep minimized via edge_keeper. Driving a backgrounded
    # CDP Edge has proven flaky (the tab renderer can be discarded -> TargetClosedError),
    # so this is opt-in. Default is the stable foreground mode.
    [switch]$Background,
    # Just bring the (already-running) companion Edge to the foreground -- used when
    # sign-in is required. Does not launch anything.
    [switch]$Surface
)

$ErrorActionPreference = "Stop"

$dataDir = Join-Path $env:LOCALAPPDATA "copilot-companion-edge"

# --- Win32 window helpers (hide to background / surface for auth) --------------
# Find() enumerates ALL top-level windows (including HIDDEN ones) belonging to the
# companion Edge process -- necessary because once SW_HIDE'd the window no longer shows
# up as Get-Process.MainWindowHandle (Windows only reports VISIBLE main windows).
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class Cw {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
  delegate bool EnumProc(IntPtr h, IntPtr p);
  public static IntPtr Find(int[] pids) {
    IntPtr found = IntPtr.Zero;
    HashSet<int> set = new HashSet<int>(pids);
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains((int)pid)) {
        StringBuilder sb = new StringBuilder(64); GetClassName(h, sb, 64);
        if (sb.ToString() == "Chrome_WidgetWin_1" && GetWindowTextLength(h) > 0) { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
function Get-CompanionWindow {
    $pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
              Where-Object { $_.CommandLine -match 'copilot-companion-edge' } |
              ForEach-Object { [int]$_.ProcessId })
    if ($pids.Count -eq 0) { return [IntPtr]::Zero }
    return [Cw]::Find($pids)
}

if ($Surface) {
    $h = Get-CompanionWindow
    if ($h -ne [IntPtr]::Zero) {
        [Cw]::ShowWindow($h, 5) | Out-Null    # SW_SHOW   (un-hide)
        [Cw]::ShowWindow($h, 9) | Out-Null    # SW_RESTORE (un-minimize + activate)
        [Cw]::SetForegroundWindow($h) | Out-Null
        Write-Host "Companion Edge brought to the foreground."
    } else {
        Write-Host "No companion Edge window found (is it running?)."
    }
    exit 0
}

if ($HardReset) {
    Write-Host "HardReset: killing companion Edge and clearing session-restore state ..."
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -match 'copilot-companion-edge' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 2
    # Deleting these makes Edge come up clean instead of restoring the wedged tabs.
    $def = Join-Path $dataDir "Default"
    foreach ($n in @("Current Session", "Current Tabs", "Last Session", "Last Tabs")) {
        $f = Join-Path $def $n
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }
    $sess = Join-Path $def "Sessions"
    if (Test-Path $sess) { Remove-Item $sess -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "HardReset: session state cleared."
}

# Idempotent: if something is already listening on the port, assume the companion
# Edge is up and do nothing (avoid spawning a second instance / fighting for the port).
$listening = $false
try {
    $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} catch { }
if ($listening) {
    Write-Host "Companion Edge already reachable on port $Port (http://127.0.0.1:$Port). Nothing to do."
    exit 0
}

# Locate msedge.exe (standard install paths, then PATH).
$edge = $null
$candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
foreach ($c in $candidates) { if (Test-Path $c) { $edge = $c; break } }
if (-not $edge) {
    $cmd = Get-Command msedge.exe -ErrorAction SilentlyContinue
    if ($cmd) { $edge = $cmd.Source }
}
if (-not $edge) { throw "msedge.exe not found. Install Microsoft Edge or pass its path." }

$arguments = @(
    "--user-data-dir=$dataDir",
    "--remote-debugging-port=$Port",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-allow-origins=*",
    # suppress the "restore pages?" / crashed-session bubble so a kill+relaunch comes up
    # clean (recovery normally closes tabs one by one via relay\edge_recover.py)
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    # keep the tab fully active even while the window is minimized in the background,
    # so the agent's streaming/turn-detection is not throttled
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    $Url
)

Write-Host "Launching dedicated companion Edge:"
Write-Host "  exe:     $edge"
Write-Host "  profile: $dataDir   (isolated from your main Edge)"
Write-Host "  port:    $Port"
Start-Process -FilePath $edge -ArgumentList $arguments | Out-Null

function Hide-Companion {
    if (-not $Background) { return }
    $h = Get-CompanionWindow
    # SW_MINIMIZE (6), NOT SW_HIDE: fully hiding makes Edge discard the tab renderer
    # (the driver then hits TargetClosedError). Minimized is stable and CDP keeps driving
    # it; edge_keeper keeps it minimized so it stays out of the way after launch.
    if ($h -ne [IntPtr]::Zero) { [Cw]::ShowWindow($h, 6) | Out-Null }
}

# Hide from the very first moment the window exists, and keep hiding while CDP comes up
# so any re-show/flash during launch + initial page load is swallowed immediately.
$deadline = (Get-Date).AddSeconds(30)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Hide-Companion
    try { $ready = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) } catch { }
    if ($ready) { break }
    Start-Sleep -Milliseconds 200
}
# (background mode only) keep minimizing briefly while M365 finishes loading.
if ($Background) {
    $extra = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $extra) { Hide-Companion; Start-Sleep -Milliseconds 250 }
}

if ($ready) {
    Write-Host ""
    Write-Host "Ready: CDP endpoint is up on http://127.0.0.1:$Port"
    if ($Background) {
        # True background via a SEPARATE virtual desktop. This is the robust approach:
        # CDP is desktop-independent, so the tab keeps running and the send path works
        # while the window simply is not on the user's current desktop. This avoids the
        # SW_HIDE-discards-renderer failure and is cleaner than perpetual minimizing.
        $mover = Join-Path $PSScriptRoot "move_companion_to_desktop.ps1"
        if (Test-Path $mover) {
            try {
                & $mover
            } catch {
                Write-Host "move_companion_to_desktop.ps1 failed: $($_.Exception.Message)"
                Write-Host "Falling back to the minimize keeper."
            }
        } else {
            Write-Host "move_companion_to_desktop.ps1 not found; using the minimize keeper only."
        }
        # Belt-and-suspenders: also run the minimize keeper, so that IF the window ever
        # lands back on the current desktop (e.g. the user removes the 2nd desktop) it is
        # still kept out of the way. Kill any prior keeper first.
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
            Where-Object { $_.CommandLine -match 'edge_keeper.ps1' } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
        $keeper = Join-Path $PSScriptRoot "edge_keeper.ps1"
        Start-Process powershell -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $keeper, "-Port", "$Port") | Out-Null
        Write-Host "Running in the background on a separate virtual desktop. If sends start"
        Write-Host "failing, relaunch WITHOUT -Background (foreground is the stable mode)."
    } else {
        Write-Host "Visible (stable mode). Sign in to M365 here if this profile is new."
    }
    exit 0
}
Write-Host "Edge launched, but the CDP port did not come up within 30s. Try -Surface."
exit 1
