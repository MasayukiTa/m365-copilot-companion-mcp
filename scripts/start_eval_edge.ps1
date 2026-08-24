# start_eval_edge.ps1 -- (re)launch the measurement series' own Edge on :9224.
#
# WHY A BROWSER OF ITS OWN. The fleet's Edge holds a resident Copilot page that belongs to no
# arm. Across six arms of a diagnostic block its top mover was that same long-lived renderer
# every time -- swinging 24, 78, 179, 239 and 182 MB -- while the process each arm actually
# created stayed at 18-20 MB. No statistic can split one process's commit between two tenants,
# so the experiment moved instead of the arithmetic.
#
# WHY RELAUNCHING MATTERS, AND NOT ONLY AT THE START. Closing a Copilot tab does not give the
# memory back. Measured on this profile: three cycles of opening four tabs and closing them left
# +422, +305 and +160 MB behind, and the browser's settled baseline climbed 523 -> 863 -> 1084 MB.
# Twenty runs of that would both exhaust the machine and move the baseline every arm is measured
# against, so the series starts each run from a browser that has just been rebuilt.
#
# OFF-SCREEN, NOT MERELY MINIMISED. Opening a page RAISES the window, and it does so faster than
# edge_keeper's two-second re-minimise -- which is exactly what the operator saw: a Copilot
# window in front of their work with no taskbar button (the taskbar half was already fixed).
# The other managed Edges on this machine sit at (-32000,-32000); this one was launched without
# a position and landed at (263,37). A window off-screen can be raised all it likes.
#
# ASCII / ENGLISH ONLY.
param(
    [int]$Port = 9224,
    [string]$ProfileDir = "$env:LOCALAPPDATA\copilot-eval-edge",
    [switch]$NoWait
)

$ErrorActionPreference = "Continue"

# Only this profile's processes. Never the fleet's (:9222) or the bridge's (:9223) -- they are
# live machinery and this script is a measurement helper.
$existing = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*copilot-eval-edge*" }
foreach ($p in $existing) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
}
if ($existing) { Start-Sleep -Seconds 2 }

function Hide-EvalWindow {
    param([int]$Port)
    try {
        Add-Type @"
using System; using System.Runtime.InteropServices;
public class EvalHide {
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
}
"@ -ErrorAction SilentlyContinue
    } catch {}
    # SW_MINIMIZE (6), never SW_HIDE as a final state: a fully hidden window makes Edge discard
    # the tab's renderer and the next CDP call dies. Same reasoning as edge_keeper's loop.
    $procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*copilot-eval-edge*" }
    foreach ($p in $procs) {
        $pr = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        if (-not $pr -or $pr.MainWindowHandle -eq 0) { continue }
        $h = $pr.MainWindowHandle
        [void][EvalHide]::ShowWindow($h, 6)
        $ex = [EvalHide]::GetWindowLong($h, -20)
        # WS_EX_TOOLWINDOW takes it out of the taskbar and Alt+Tab; clearing WS_EX_APPWINDOW
        # stops it being put back. Minimising alone keeps the taskbar button, which is the part
        # the operator actually sees.
        [void][EvalHide]::SetWindowLong($h, -20, ($ex -bor 0x80) -band (-bnot 0x40000))
        [void][EvalHide]::SetWindowPos($h, [IntPtr]::Zero, -32000, -32000, 0, 0, 0x0001 -bor 0x0004 -bor 0x0020)
        [void][EvalHide]::ShowWindow($h, 6)
    }
}


$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "C:\Program Files\Microsoft\Edge\Application\msedge.exe" }
if (-not (Test-Path $edge)) { Write-Host "msedge.exe not found"; exit 1 }

# The flag set is copied from the instance this replaces, deliberately and without edits, so a
# rebuilt browser measures the same thing as the one before it. --process-per-site in particular
# is left alone: it consolidates same-site pages into one renderer, which is what the production
# fleet does, and removing it would change the quantity rather than clean it up.
$flags = @(
    "--window-size=1400,1000",
    "--window-position=-32000,-32000",
    "--user-data-dir=$ProfileDir",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=$Port",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-extensions",
    "--disable-sync",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--process-per-site"
)

Start-Process -FilePath $edge -ArgumentList $flags -WindowStyle Minimized | Out-Null

if ($NoWait) { exit 0 }

# Wait for CDP rather than sleeping a guessed number of seconds: the series' next act is to
# connect, and a fixed sleep is how a slow start becomes a mysterious connection failure.
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) {
            # HIDE IT HERE, NOT ONLY IN THE KEEPER. edge_keeper marks these windows
            # WS_EX_TOOLWINDOW and re-minimises them every two seconds, but it was not running
            # when this was written -- the check that said it was had matched its own query's
            # command line -- and a freshly launched window sat in front of the operator with a
            # taskbar button until somebody noticed. A launcher that leaves its own window
            # showing is relying on a watcher it does not start.
            Hide-EvalWindow -Port $Port
            Write-Host "eval edge up on :$Port"
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 700
}
Write-Host "eval edge did not answer on :$Port within 45s"
exit 1
