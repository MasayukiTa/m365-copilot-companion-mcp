# eval_windows.ps1 -- report which of a profile's windows a human could actually see.
#
# READ-ONLY. It starts nothing, kills nothing, and changes no window. That matters: the
# monitoring command that was supposed to CHECK visibility during a run was written to call the
# LAUNCHER, which kills the eval browser and relaunches it. Had it run, it would have destroyed
# the measurement it was watching -- the third time in one day that a command aimed at this
# profile hit something it was not meant to touch.
#
# ONE DEFINITION, TWO CALLERS. The launcher refuses to start when this says a window is showing;
# a monitor prints the same thing during a run. Copying the predicate into both is how the two
# drift until one of them quietly says "hidden" about a window sitting in front of somebody.
#
# A window of this profile is acceptable only when it is off screen AND out of the taskbar.
# The one legitimate exception is a sign-in surface, and deciding that is a human's call, not
# this file's -- so it reports, and the caller decides.
#
# Run directly to see the verdict:  powershell -File scripts\win\eval_windows.ps1
# Dot-source to reuse the function: . scripts\win\eval_windows.ps1
#
# ASCII / ENGLISH ONLY.
param([string]$EdgeProfile = "copilot-eval-edge", [switch]$Quiet)

$ErrorActionPreference = "SilentlyContinue"

function Get-VisibleEvalWindows {
    param([string]$Marker)
    Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class EvalWin {
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public struct RECT { public int L,T,R,B; }
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  public static System.Collections.Generic.List<IntPtr> All = new System.Collections.Generic.List<IntPtr>();
  public static bool Cb(IntPtr h, IntPtr l) { All.Add(h); return true; }
}
"@ -ErrorAction SilentlyContinue
    $pids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -like "*$Marker*" } | ForEach-Object { [int]$_.ProcessId })
    [EvalWin]::All.Clear()
    [void][EvalWin]::EnumWindows([EvalWin+EnumProc]{ param($h,$l) [EvalWin]::Cb($h,$l) }, [IntPtr]::Zero)
    $bad = @()
    foreach ($h in [EvalWin]::All) {
        $p = 0; [void][EvalWin]::GetWindowThreadProcessId($h, [ref]$p)
        if ($pids -notcontains $p) { continue }
        if ([EvalWin]::GetParent($h) -ne [IntPtr]::Zero) { continue }
        if (-not [EvalWin]::IsWindowVisible($h)) { continue }
        $r = New-Object EvalWin+RECT
        [void][EvalWin]::GetWindowRect($h, [ref]$r)
        $ex = [EvalWin]::GetWindowLong($h, -20)
        $onScreen = ($r.L -gt -30000)
        $inTaskbar = (($ex -band 0x80) -eq 0)
        if ($onScreen -or $inTaskbar) {
            $sb = New-Object System.Text.StringBuilder 100
            [void][EvalWin]::GetWindowText($h, $sb, 100)
            $bad += ("hwnd=" + $h + " onScreen=" + $onScreen + " inTaskbar=" + $inTaskbar +
                     " title=" + $sb.ToString())
        }
    }
    return $bad
}

# Dot-sourcing sets $MyInvocation.InvocationName to "."; only a direct run should print.
if ($MyInvocation.InvocationName -ne "." -and -not $Quiet) {
    $showing = Get-VisibleEvalWindows -Marker $EdgeProfile
    if ($showing.Count -eq 0) {
        Write-Host "no window of this profile is visible"
    } else {
        Write-Host ("VISIBLE: " + $showing.Count + " window(s) the operator could see:")
        foreach ($w in $showing) { Write-Host ("  " + $w) }
    }
}
