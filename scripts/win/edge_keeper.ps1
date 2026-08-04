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

param([int]$Port = 9222)

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

while ($true) {
    Start-Sleep -Seconds 2

    # back off while sign-in is being surfaced
    if (Test-Path $pauseFile) {
        $age = (New-TimeSpan -Start (Get-Item $pauseFile).LastWriteTime -End (Get-Date)).TotalSeconds
        if ($age -lt 180) { continue }
    }

    $pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
              Where-Object { $_.CommandLine -match 'copilot-companion-edge' } |
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
    $h = [K]::Find($pids)
    if ($h -ne [IntPtr]::Zero -and [K]::IsWindowVisible($h) -and -not [K]::IsIconic($h)) {
        [K]::ShowWindow($h, 6) | Out-Null
    }
}
