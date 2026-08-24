# start_eval_edge.ps1 -- (re)launch the measurement series' Edge on :9224.
#
# THIS IS A THIN WRAPPER, AND IT USED NOT TO BE. That was the mistake.
#
# The first version launched Edge itself and tried to keep the window out of the way by
# minimising it and marking it WS_EX_TOOLWINDOW. Both halves were wrong, and the second half was
# worse than doing nothing:
#
#   * The shell decides whether a window gets a taskbar button AT THE MOMENT IT IS SHOWN.
#     Changing the ex-style of an already-visible window does not make Explorer re-evaluate, so
#     the button minted at first show survives. The correct sequence is hide, change the style,
#     show again -- and that function contained no SW_HIDE at all.
#   * Setting the 0x80 bit made edge_keeper.ps1 and edge_recover.rehide() -- both of which DO use
#     the correct bracket -- skip the window, because both guard on `if (($ex -band 0x80) -eq 0)`.
#     The launcher's marking inoculated the window against the two things that would have fixed
#     it. The keeper ran, rehide() was called by hand, and neither did anything, for that reason.
#
# The project already had the answer. start_companion_edge.ps1 defaults to --headless=new --
# "no window, no taskbar, zero foreground interference", with CDP, SSO and sends all working --
# and parks the headless window surfaces off-screen. Its own comments call a separate desktop or
# headless "cleaner than perpetual minimizing". With no window there is no race against the
# first show and nothing to mark.
#
# WHY THIS FILE STILL EXISTS: the series needs ONE call that rebuilds the browser between runs
# with the profile and port fixed, and needs it to fail loudly. Wrapping keeps that in one place
# instead of re-implementing a launcher that already works.
#
# ASCII / ENGLISH ONLY.
param(
    [int]$Port = 9224,
    [string]$EdgeProfile = "copilot-eval-edge"
)

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot

# Kill this profile's instance, and ONLY this profile's. The fleet (:9222) and the bridge
# (:9223) are live machinery; this is a measurement helper and must never touch them.
#
# The rebuild is not tidiness. Closing a Copilot tab does not give the memory back: three
# open-four-close cycles on this profile left +422, +305 and +160 MB behind and the settled
# baseline climbed 523 -> 863 -> 1084 MB. Over twenty runs that exhausts the machine and moves
# the baseline every arm is measured against.
$existing = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$EdgeProfile*" }
foreach ($p in $existing) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
}
if ($existing) { Start-Sleep -Seconds 2 }

$launcher = Join-Path $repoRoot "scripts\start_companion_edge.ps1"
if (-not (Test-Path $launcher)) {
    Write-Host "start_companion_edge.ps1 not found"
    exit 1
}

# No -Foreground: the launcher's $useHeadless is `-not $Foreground`, so OMITTING the switch is
# what selects headless. Said out loud because an absent switch reads like an oversight.
& powershell -NoProfile -ExecutionPolicy Bypass -File $launcher -Port $Port -Profile $EdgeProfile
$code = $LASTEXITCODE

# PROVE IT IS NOT SHOWING, RATHER THAN ASSUME IT. Three fixes in a row were believed to have
# worked and had not; the operator found each one. A window this profile owns is acceptable only
# when it is off-screen AND out of the taskbar, and the one legitimate exception is a sign-in
# surface, which is the single case where a human must see it. Anything else fails the launch,
# because a measurement that runs while a window sits in front of somebody is not worth its data.
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

try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5 -UseBasicParsing
    if ($r.StatusCode -eq 200) {
        $showing = Get-VisibleEvalWindows -Marker $EdgeProfile
        if ($showing.Count -gt 0) {
            Write-Host "REFUSING: this profile has a window the operator could see:"
            foreach ($w in $showing) { Write-Host ("  " + $w) }
            Write-Host "A sign-in surface is the only case where that is allowed, and it is not"
            Write-Host "this script's job to decide that -- relaunch with -Foreground to sign in."
            exit 2
        }
        Write-Host "eval edge up on :$Port (headless, no visible window)"
        exit 0
    }
} catch {}

Write-Host "eval edge did not answer on :$Port (launcher exit $code)"
exit 1
