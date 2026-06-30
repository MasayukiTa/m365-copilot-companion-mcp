# move_companion_to_desktop.ps1
# Move the dedicated companion Edge window onto a SEPARATE Windows virtual desktop so
# it is truly out of sight (a real background), without minimizing/hiding it.
#
# Why a separate desktop instead of minimize/SW_HIDE:
#   Hiding (SW_HIDE) makes Edge discard the tab renderer -> CDP TargetClosedError.
#   Minimizing keeps re-popping and is only "mostly" out of the way. A virtual desktop
#   is desktop-independent for CDP: the tab keeps running, the send path is focus- and
#   desktop-independent, and the window simply is not on the user's current desktop.
#
# What this does:
#   1. Find the companion Edge top-level window HANDLE (EnumWindows, msedge process whose
#      CommandLine contains 'copilot-companion-edge', class Chrome_WidgetWin_1, titled).
#   2. Ensure at least 2 virtual desktops exist (VirtualDesktop.exe /New if only 1).
#   3. Move that window handle to desktop index 1 (the 2nd desktop) WITHOUT switching the
#      user's current desktop (no /Switch).
#
# It prints what it did. It does NOT relaunch or kill Edge.
#
# VirtualDesktop.exe (MScholtes/VirtualDesktop, VirtualDesktop11.cs, Windows 11) switches:
#   /Count                          -> count of desktops (also as error level)
#   /New                            -> create a new desktop
#   /GetDesktop:<n>                 -> put desktop number <n> into the pipeline
#   /MoveWindowHandle:<hwnd>        -> move window <hwnd> to the desktop number in pipeline
#   /GetDesktopFromWindowHandle:<hwnd> -> desktop number a window is on (also as error level)
#
# ASCII / ENGLISH ONLY (project rule).

param(
    # Target desktop index to move the companion window to (0-based). Default 1 = 2nd desktop.
    [int]$TargetIndex = 1
)

$ErrorActionPreference = "Stop"

$vd = Join-Path $PSScriptRoot "thirdparty\VirtualDesktop.exe"
if (-not (Test-Path $vd)) {
    # auto-build from the vendored source (csc ships with Windows; no SDK needed)
    $src = Join-Path $PSScriptRoot "thirdparty\VirtualDesktop11.cs"
    $csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if ((Test-Path $src) -and (Test-Path $csc)) {
        Write-Host "Building VirtualDesktop.exe from source ..."
        & $csc /nologo /target:exe /out:$vd $src | Out-Null
    }
    if (-not (Test-Path $vd)) {
        Write-Host "ERROR: VirtualDesktop.exe missing and could not be built from $src"
        exit 1
    }
}

# --- Find the companion Edge top-level window handle (same technique as edge_keeper.ps1) ---
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class CwMove {
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

$pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
          Where-Object { $_.CommandLine -match 'copilot-companion-edge' } |
          ForEach-Object { [int]$_.ProcessId })
if ($pids.Count -eq 0) {
    Write-Host "No companion Edge process found (CommandLine contains 'copilot-companion-edge'). Is it running?"
    exit 1
}

$h = [CwMove]::Find($pids)
if ($h -eq [IntPtr]::Zero) {
    Write-Host "Companion Edge process is running but no titled top-level window was found yet."
    exit 1
}
$hwnd = [int64]$h
Write-Host ("Companion Edge window handle: {0} (0x{0:X})" -f $hwnd)

# --- Ensure at least (TargetIndex + 1) desktops exist ----------------------------
# /Count returns the count as the process error level. Parse the printed text too as a
# robust fallback (verbose mode prints "Count of desktops: N").
function Get-DesktopCount {
    $out = & $vd /Count 2>&1
    $code = $LASTEXITCODE
    $m = ($out | Select-String -Pattern 'Count of desktops:\s*(\d+)' | Select-Object -First 1)
    if ($m) { return [int]$m.Matches[0].Groups[1].Value }
    return [int]$code
}

$count = Get-DesktopCount
Write-Host "Current virtual desktop count: $count"

$needed = $TargetIndex + 1
while ($count -lt $needed) {
    Write-Host "Creating a new virtual desktop (have $count, need $needed) ..."
    & $vd /New | Out-Null
    $count = Get-DesktopCount
}

# --- Where is it now? ------------------------------------------------------------
$beforeOut = & $vd ("/GetDesktopFromWindowHandle:{0}" -f $hwnd) 2>&1
$beforeCode = $LASTEXITCODE
Write-Host ("Before move: {0} (desktop index {1})" -f ($beforeOut -join ' '), $beforeCode)

# --- Move to the target desktop WITHOUT switching the user's current desktop ------
# Pipeline: put target desktop number into pipeline (/GetDesktop:<n>), then move the
# handle to "the desktop number in pipeline" (/MoveWindowHandle:<hwnd>). No /Switch.
Write-Host ("Moving window {0} to desktop index {1} ..." -f $hwnd, $TargetIndex)
& $vd ("/GetDesktop:{0}" -f $TargetIndex) ("/MoveWindowHandle:{0}" -f $hwnd) | Out-Null

# --- Confirm ---------------------------------------------------------------------
$afterOut = & $vd ("/GetDesktopFromWindowHandle:{0}" -f $hwnd) 2>&1
$afterCode = $LASTEXITCODE
Write-Host ("After move:  {0} (desktop index {1})" -f ($afterOut -join ' '), $afterCode)

if ($afterCode -eq $TargetIndex) {
    Write-Host ("OK: companion Edge is now on desktop index {0} (off the current desktop)." -f $TargetIndex)
    exit 0
} else {
    Write-Host ("WARNING: expected desktop index {0} but got {1}." -f $TargetIndex, $afterCode)
    exit 1
}
