# edge_keeper.ps1
# Persistent background watcher: keeps the dedicated companion Edge HIDDEN.
#
# A one-shot hide does not stick -- Edge re-shows its window when the page finishes
# loading / on focus changes. This loop re-hides it every ~2s, so the user never sees
# it. CDP keeps driving it while hidden, and the send path is focus-independent, so the
# user can work in other apps normally. When sign-in is needed, surface() writes a pause
# file (.fleet\edge_keep_pause) and this loop backs off so the window can stay up.
#
# ASCII / ENGLISH ONLY. Started (and re-started) by start_companion_edge.ps1.

# $ProfileMarker is a regex matched against msedge.exe command lines. It defaults to BOTH
# dedicated profiles: the fleet Edge (copilot-companion-edge, :9222) AND the interactive
# bridge Edge (copilot-bridge-edge, :9223). Hardcoding the companion profile left the
# bridge window entirely unwatched, so a bridge window that popped back up stayed up --
# the loop that is supposed to keep these windows out of the way simply never saw it.
# The eval Edge (:9224, copilot-eval-edge) was the FOURTH profile added without sweeping the
# places that enumerate profiles, and the symptom was the one this loop exists to prevent: a
# window sitting in front of the operator with nothing watching it. The list now lives in
# relay/edge_recover.py's MANAGED_EDGE_PROFILES; keep this default in step with it.
param([int]$Port = 9222,
      [string]$ProfileMarker = 'copilot-companion-edge|copilot-bridge-edge|copilot-eval-edge')

$ErrorActionPreference = "SilentlyContinue"

# The pause file lives at <repo-root>\.fleet\edge_keep_pause, written by
# edge_recover.surface()/touch_pause(). This script is at <repo-root>\scripts\win,
# so resolve the repo root by walking TWO directories up from $PSScriptRoot.
# (A plain "$PSScriptRoot\.fleet" would look in scripts\win\.fleet, which never
# exists, so the pause would never take effect and this loop would fight the user
# mid-login.)
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$fleet = Join-Path $repoRoot ".fleet"
$pauseFile = Join-Path $fleet "edge_keep_pause"

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class K {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
  delegate bool EnumProc(IntPtr h, IntPtr p);
  // ALL matching windows, not just the first. One Edge process can own several
  // top-level windows (a second window, a detached tab, a picture-in-picture). The
  // previous version returned on the first hit, so every window after it was left
  // untouched -- and one stray visible window is all it takes for the operator to see
  // a companion Edge they are never supposed to see.
  public static IntPtr[] FindAll(int[] pids) {
    List<IntPtr> found = new List<IntPtr>();
    HashSet<int> set = new HashSet<int>(pids);
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains((int)pid)) {
        StringBuilder sb = new StringBuilder(64); GetClassName(h, sb, 64);
        if (sb.ToString() == "Chrome_WidgetWin_1" && GetWindowTextLength(h) > 0) { found.Add(h); }
      }
      return true;
    }, IntPtr.Zero);
    return found.ToArray();
  }
}
"@

# DISK, NOT JUST WINDOWS. This is the only loop that already watches all three managed
# profiles, so it is where the cache ceiling gets checked. Nothing checked it before: the value
# that reads like a disk cap (edge_recover.RECYCLE_EDGE_CAP_MB, 1500) is compared against RAM,
# and the trim that does delete cache is fail-closed on "is this profile running" -- which the
# fleet Edge and the bridge Edge always are. A cap that never fired and a trim that always
# declined let 1.45 GB accumulate across two browsers holding one chat origin.
#
# Every five minutes, not every two seconds: the check walks the cache directories, and the
# thing it is guarding against takes hours to happen.
$capEvery = 300
$lastCap = (Get-Date).AddSeconds(-$capEvery)

while ($true) {
    Start-Sleep -Seconds 2

    if ((New-TimeSpan -Start $lastCap -End (Get-Date)).TotalSeconds -ge $capEvery) {
        $lastCap = Get-Date
        # Out-of-process on purpose: the ceiling, the worker count it is derived from, and the
        # rule about never deleting under a running browser all live in one Python module with
        # tests. A PowerShell re-implementation here would be a second copy of the directory
        # list, which is exactly how GrShaderCache came to be missed on one side only.
        $py = Join-Path $repoRoot ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { $py = "python" }
        # -m resolves against the CURRENT directory, and this loop is started from wherever the
        # launcher happened to be. Without the push it silently fails to import relay every
        # time -- the loop keeps running, the windows keep being minimized, and the cap that
        # was added is never once applied.
        Push-Location $repoRoot
        try { & $py -m relay.edge_disk_cap 2>&1 | Out-Null } catch { } finally { Pop-Location }
    }

    # back off while sign-in is being surfaced
    if (Test-Path $pauseFile) {
        $age = (New-TimeSpan -Start (Get-Item $pauseFile).LastWriteTime -End (Get-Date)).TotalSeconds
        if ($age -lt 180) { continue }
    }

    $pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
              Where-Object { $_.CommandLine -match $ProfileMarker } |
              ForEach-Object { [int]$_.ProcessId })
    if ($pids.Count -eq 0) { continue }

    # SW_MINIMIZE (6), NOT SW_HIDE: a fully-hidden window makes Edge discard the tab's
    # renderer (TargetClosedError mid-drive). Minimized is stable -- CDP keeps driving it
    # and the send path is focus-independent. Re-minimize only if it has popped back up
    # AND it is currently visible: a headless (--headless=new) window has WS_VISIBLE
    # clear, and calling ShowWindow(SW_MINIMIZE) on such a window makes Windows SET
    # WS_VISIBLE and show it minimized -- which is exactly how a taskbar button appears
    # on a companion Edge that is supposed to have no window at all. A window that is not
    # visible needs no minimizing; touching it is what reveals it.
    if ($null -eq $script:HandledWindows) { $script:HandledWindows = @{} }
    foreach ($h in [K]::FindAll($pids)) {
        if ($h -eq [IntPtr]::Zero) { continue }
        if ([K]::IsWindowVisible($h) -and -not [K]::IsIconic($h)) {
            [K]::ShowWindow($h, 6) | Out-Null
        }
        # Minimizing is not enough. A minimized window KEEPS its taskbar button, and the
        # taskbar button is the thing the operator actually sees -- reported twice on
        # 2026-08-10, both times right after the bridge was restarted. WS_EX_TOOLWINDOW
        # takes the window out of the taskbar and Alt+Tab while leaving it live and
        # drivable over CDP (measured on the bridge Edge: :9223 kept answering with the
        # flag set).
        #
        # Until now this loop only minimized, and the flag was set solely by
        # relay/edge_recover.py's rehide() -- which runs on RECOVERY, not at launch. So a
        # freshly launched Edge had nobody to mark it, and every restart put the window
        # back in the taskbar until something happened to trigger a recovery. This loop is
        # the only thing that watches continuously, so it is where the flag belongs.
        #
        # The style change needs the window hidden for the instant it is applied; that is
        # why SW_HIDE (0) appears here. It is never the FINAL state -- leaving it hidden
        # makes Edge discard the tab's renderer and the next CDP call dies with
        # TargetClosedError. Same sequence, same reasoning as _REHIDE_PS.
        # THE BIT IS NOT PROOF OF ABSENCE FROM THE TASKBAR. The shell decides membership at
        # FIRST SHOW and does not re-read the style afterwards, so a window can carry
        # WS_EX_TOOLWINDOW and still own a button minted before the bit went on -- which is
        # exactly what a window marked by something else, or by an earlier keeper that died
        # between the two calls, looks like. Skipping on the bit alone then leaves the button
        # the operator is complaining about sitting there, while the log says the window is
        # already handled.
        #
        # So the hide/mark/re-show sequence runs UNCONDITIONALLY the first time this keeper
        # sees a handle. Hiding and re-showing is what makes the shell re-evaluate membership;
        # the bit alone never does. After that first pass the handle is known to have been
        # shown while marked, and the cheap bit test is enough to keep the loop quiet.
        $ex = [K]::GetWindowLong($h, -20)
        $key = [string]$h
        if (-not $script:HandledWindows.ContainsKey($key) -or ($ex -band 0x80) -eq 0) {
            [K]::ShowWindow($h, 0) | Out-Null
            [K]::SetWindowLong($h, -20, ($ex -bor 0x80) -band (-bnot 0x40000)) | Out-Null
            [K]::ShowWindow($h, 6) | Out-Null
            $script:HandledWindows[$key] = $true
        }
    }
}
