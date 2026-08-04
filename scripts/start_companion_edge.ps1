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
    # Profile = the user-data-dir folder under %LOCALAPPDATA%. Default is the companion/
    # fleet Edge. The interactive BRIDGE uses a SEPARATE profile (copilot-bridge-edge) on
    # its OWN CDP port, so it can run concurrently with the fleet's :9222 Edge -- Edge locks
    # a profile to a single process, so concurrent use REQUIRES distinct profiles + ports.
    [string]$Profile = $(if ($env:MCP_EDGE_PROFILE) { $env:MCP_EDGE_PROFILE } else { "copilot-companion-edge" }),
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
    [switch]$Surface,
    # TRUE BACKGROUND (mandatory default): run Edge with --headless=new -- there is NO window
    # at all (nothing in the taskbar, no flash, zero foreground interference), yet CDP,
    # SSO auto-sign-in, and sends all work (verified). The only caveat: if interactive
    # sign-in is ever required (SSO usually persists so this is rare), relaunch once with
    # -Foreground to sign in. Foreground is deliberately one-shot: every ordinary launch,
    # -HardReset, and auto-recovery returns to headless without relying on persisted state.
    [switch]$Headless
)

$ErrorActionPreference = "Stop"

# This script lives in <repo>\scripts. $repoRoot is the REPO ROOT: the .fleet state dir and
# the scripts\win\ helpers are resolved against it (the relay reads the SAME repo-root .fleet).
$repoRoot = Split-Path -Parent $PSScriptRoot

$dataDir = Join-Path $env:LOCALAPPDATA $Profile

# Headless is the invariant recovery baseline. The marker is retained for compatibility and
# diagnostics, but a headed sign-in session is NEVER persisted: -Foreground affects only the
# explicit invocation that carries it. This also makes recovery from a separate git worktree
# headless even when that worktree has no prior .fleet state.
$modeFile = Join-Path $repoRoot ".fleet\edge_mode_$Profile"
if ($Headless -and $Foreground) { throw "-Headless and -Foreground are mutually exclusive." }
try {
    New-Item -ItemType Directory -Force (Split-Path $modeFile) | Out-Null
    Set-Content -Path $modeFile -Value "headless" -Encoding ascii
} catch {}
$useHeadless = -not $Foreground

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
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
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
  public static IntPtr[] FindChromeWidgets(int[] pids) {
    List<IntPtr> found = new List<IntPtr>();
    HashSet<int> set = new HashSet<int>(pids);
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains((int)pid)) {
        StringBuilder sb = new StringBuilder(64); GetClassName(h, sb, 64);
        string cls = sb.ToString();
        if (cls == "Chrome_WidgetWin_1" || cls == "Chrome_WidgetWin_0") { found.Add(h); }
      }
      return true;
    }, IntPtr.Zero);
    return found.ToArray();
  }
}
"@
function Get-CompanionPids {
    return @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
             Where-Object { $_.CommandLine -match [regex]::Escape($Profile) } |
             ForEach-Object { [int]$_.ProcessId })
}

function Get-CompanionWindow {
    $pids = @(Get-CompanionPids)
    if ($pids.Count -eq 0) { return [IntPtr]::Zero }
    return [Cw]::Find($pids)
}

function Park-CompanionWindowsOffscreen {
    $pids = @(Get-CompanionPids)
    if ($pids.Count -eq 0) { return 0 }
    $handles = @([Cw]::FindChromeWidgets($pids))
    $count = 0
    foreach ($h in $handles) {
        if ($h -eq [IntPtr]::Zero) { continue }
        # Headless Edge can still leave a non-interactive DWM surface. Move only this
        # dedicated profile's Chrome widgets far off-screen; do not kill Edge, hide the
        # renderer window, or touch the user's normal browser/background services.
        [Cw]::SetWindowPos($h, [IntPtr]::Zero, -32000, -32000, 0, 0, 0x0001 -bor 0x0004 -bor 0x0010) | Out-Null
        $count++
    }
    return $count
}

# True iff a msedge process for THIS profile is currently running with --headless.
# Used to decide, on a -Foreground request, whether the running instance is a headless
# one that must be killed + relaunched headed (a headless Edge holds the CDP port, so a
# plain -Foreground would hit the "already reachable -> nothing to do" early-exit and do
# nothing -- there is no window to raise).
function Test-CompanionHeadless {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
               Where-Object { $_.CommandLine -match [regex]::Escape($Profile) })
    foreach ($p in $procs) {
        if ($p.CommandLine -match '--headless') { return $true }
    }
    return $false
}

# Kill every msedge for THIS profile and wipe its session-restore state (shared by
# -HardReset and the headless->headed -Foreground swap), so the relaunch comes up clean.
function Reset-CompanionSession {
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape($Profile) } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 2
    $def = Join-Path $dataDir "Default"
    foreach ($n in @("Current Session", "Current Tabs", "Last Session", "Last Tabs")) {
        $f = Join-Path $def $n
        if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }
    $sess = Join-Path $def "Sessions"
    if (Test-Path $sess) { Remove-Item $sess -Recurse -Force -ErrorAction SilentlyContinue }
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
    Reset-CompanionSession
    Write-Host "HardReset: session state cleared."
}

# Idempotent: if something is already listening on the port, assume the companion
# Edge is up and do nothing (avoid spawning a second instance / fighting for the port).
$listening = $false
try {
    $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} catch { }
if ($listening) {
    # SPECIAL CASE (headless -> headed sign-in swap): -Foreground is a request to make a
    # window visible for interactive sign-in. A HEADLESS Edge holds the port but has NO
    # window, so the plain "already reachable -> nothing to do" exit would silently do
    # nothing. When the running instance is headless, kill it + wipe session state, then
    # fall through to a clean HEADED relaunch. (An already-HEADED instance needs no
    # relaunch -- raise it to the front via -Surface and exit.)
    if ($Foreground -and (Test-CompanionHeadless)) {
        Write-Host "Companion Edge on port $Port is HEADLESS; -Foreground requested for sign-in."
        Write-Host "Killing the headless instance and relaunching HEADED ..."
        Reset-CompanionSession
        # fall through to the launch path below (headed, since $Foreground keeps $useHeadless=$false)
    } else {
        if ($Foreground) {
            # Already headed and reachable: just bring the existing window to the front.
            $h = Get-CompanionWindow
            if ($h -ne [IntPtr]::Zero) {
                [Cw]::ShowWindow($h, 5) | Out-Null    # SW_SHOW
                [Cw]::ShowWindow($h, 9) | Out-Null    # SW_RESTORE
                [Cw]::SetForegroundWindow($h) | Out-Null
                Write-Host "Companion Edge already headed on port $Port; brought to the foreground."
            } else {
                Write-Host "Companion Edge already reachable on port $Port; no window found to raise."
            }
        } else {
            if ($useHeadless -or (Test-CompanionHeadless)) {
                $parked = Park-CompanionWindowsOffscreen
                if ($parked -gt 0) {
                    Write-Host "Companion Edge already reachable on port $Port; parked $parked headless window surface(s) off-screen."
                    exit 0
                }
            }
            Write-Host "Companion Edge already reachable on port $Port (http://127.0.0.1:$Port). Nothing to do."
        }
        exit 0
    }
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

$arguments = @()
if ($useHeadless) {
    # true background: no window at all. (verified: CDP + SSO + sends all work headless)
    $arguments += "--headless=new"
    $arguments += "--window-position=-32000,-32000"
}
# CRITICAL (verified 2026-06-30): headless=new has no real window, so WITHOUT an explicit
# viewport the M365 Copilot SPA renders at a tiny/zero size and FAILS to resolve a
# `?titleId=` custom agent -- it silently falls back to the DEFAULT Copilot (the custom
# agent like desktopfile操作 never loads, so its MCP connector tools are absent and every
# local task lands in plain Copilot). A real window-size makes headless behave IDENTICALLY
# to headed: with --window-size the desktopfile agent loads and list_directory executes in
# headless. Always pass it (harmless in headed mode, where a real window already exists).
$arguments += "--window-size=1400,1000"
$arguments += @(
    "--user-data-dir=$dataDir",
    # CDP has no built-in authentication. Keep it reachable only by local
    # processes; remote users still reach the MCP server through Dev Tunnel.
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=$Port",
    "--no-first-run",
    "--no-default-browser-check",
    # suppress the "restore pages?" / crashed-session bubble so a kill+relaunch comes up
    # clean (recovery normally closes tabs one by one via relay\edge_recover.py)
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    # keep the tab fully active even while the window is minimized in the background,
    # so the agent's streaming/turn-detection is not throttled
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # trim memory footprint (this profile only ever shows M365 Copilot). These are safe
    # for SSO -- we deliberately do NOT touch background-networking, which token refresh
    # can use. process-per-site consolidates M365's same-site tabs into one renderer.
    "--disable-extensions",
    "--disable-sync",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--process-per-site",
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
    # it; edge_keeper keeps it minimized so it stays out of the way after launch. Only
    # minimize a window that is actually visible and not already minimized: a headless
    # (WS_VISIBLE clear) window has no window to minimize, and ShowWindow(SW_MINIMIZE) on
    # it makes Windows SET WS_VISIBLE and show it minimized -- creating a taskbar button
    # on what is supposed to be a windowless instance. Mirrors the same guard in
    # scripts\win\edge_keeper.ps1 / relay\edge_recover.py's _REHIDE_PS.
    if ($h -ne [IntPtr]::Zero -and [Cw]::IsWindowVisible($h) -and -not [Cw]::IsIconic($h)) {
        [Cw]::ShowWindow($h, 6) | Out-Null
    }
}

# Hide from the very first moment the window exists, and keep hiding while CDP comes up
# so any re-show/flash during launch + initial page load is swallowed immediately.
$deadline = (Get-Date).AddSeconds(30)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if ($useHeadless) { Park-CompanionWindowsOffscreen | Out-Null }
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
if ($useHeadless) {
    $extra = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $extra) { Park-CompanionWindowsOffscreen | Out-Null; Start-Sleep -Milliseconds 250 }
}

if ($ready) {
    Write-Host ""
    Write-Host "Ready: CDP endpoint is up on http://127.0.0.1:$Port"
    if ($useHeadless) {
        Write-Host "Running TRUE BACKGROUND (headless: no window, no taskbar, zero foreground"
        Write-Host "interference). CDP + SSO + sends all work. If sign-in is ever needed,"
        Write-Host "relaunch once with -Foreground to sign in, then -Headless again."
    } elseif ($Background) {
        # True background via a SEPARATE virtual desktop. This is the robust approach:
        # CDP is desktop-independent, so the tab keeps running and the send path works
        # while the window simply is not on the user's current desktop. This avoids the
        # SW_HIDE-discards-renderer failure and is cleaner than perpetual minimizing.
        $mover = Join-Path $repoRoot "scripts\win\move_companion_to_desktop.ps1"
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
        $keeper = Join-Path $repoRoot "scripts\win\edge_keeper.ps1"
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
