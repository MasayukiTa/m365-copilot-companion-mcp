# WHY A SUPERVISOR EXISTS, AND WHY THE FIRST ONE DID NOT WORK.
#
# 2026-08-29 19:17: the driver printed "DONE" with 23 of 40 instances unanswered, exited,
# and the machine sat idle for three hours. The answer to that was a supervisor -- and the
# supervisor made the next night worse, not better:
#
#   23:03-23:58  every submit failed with "no writable text field found in the cockpit"
#                (the window search matched a WPF popup instead of the main window)
#   23:03-23:59  the supervisor relaunched the driver eight times. Each new driver failed
#                the same way in four minutes, because nothing about restarting a process
#                repairs the thing that is blocking it.
#   23:59:41     "8 relaunches already spent; stopping"  <- a stop button I installed
#   23:59-06:45  six hours and forty-six minutes of silence.
#
# So the lesson is not "retry harder". It is:
#
#   1. A RESTART IS NOT A REMEDY. Before relaunching, check the condition that actually
#      blocks submission and repair THAT: the composer must be reachable. Restore the
#      window, and if that does not help, restart the cockpit.
#   2. REMEDIES ESCALATE. Repeating the remedy that just failed is how eight launches
#      produced nothing. Each failed cycle moves to a stronger one.
#   3. THERE IS NO GIVE-UP COUNT WHILE WORK REMAINS. A ceiling on attempts is a scheduled
#      stall. When nothing works, this slows down and keeps saying so; it does not exit.
[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice40_fresh.json",
    [string]$Preds     = ".fleet/swe/ui_preds.json",
    [string]$Log       = ".fleet/swe/ui_supervisor.log",
    [int]$CheckSec     = 60
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

# CAN A SUBMIT REACH THE COMPOSER? -ReadOnly prints what it found and submits nothing, so
# this is a probe and not a side effect. The string is the composer's automation id, which
# is what the submit path actually needs to exist.
function Test-Composer {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\submit_via_ui.ps1 `
                -GoalFile ".fleet/swe/ui_batch_lines.txt" -ReadOnly 2>&1 | Out-String
    return ($out -match 'goalInput')
}

function Repair-Cockpit([int]$level) {
    if ($level -le 1) {
        Say "repair 1: restore and foreground the cockpit window"
        $p = Get-Process -Name FleetCockpit -EA SilentlyContinue | Select-Object -First 1
        if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {
            [Win32.Sup]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
            [Win32.Sup]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
        } else { Say "  no cockpit main window handle to restore" }
        Start-Sleep -Seconds 5
        return
    }
    # RESTARTING THE COCKPIT IS NOT FREE: it drops the window a person may be reading and
    # any run in flight, so it is the second remedy, never the first.
    Say "repair 2: restart the cockpit"
    Get-Process -Name FleetCockpit -EA SilentlyContinue | Stop-Process -Force -EA SilentlyContinue
    Start-Sleep -Seconds 4
    $exe = Join-Path $repo "ui\FleetCockpit.exe"
    if (Test-Path $exe) { Start-Process -FilePath $exe | Out-Null; Start-Sleep -Seconds 12 }
    else { Say "  ui\FleetCockpit.exe not found; cannot restart" }
}

if (-not ('Win32.Sup' -as [type])) {
    Add-Type -Namespace Win32 -Name Sup -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool SetForegroundWindow(System.IntPtr hWnd);
'@ -PassThru | Out-Null
}

$idsFile = ".fleet/swe/ui_all_ids.txt"
if (-not (Test-Path $idsFile)) {
    $ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); print('\n'.join(sorted(x['instance_id'] for x in r)))" $SliceFile
    Set-Content -Path $idsFile -Value $ids -Encoding ascii
}

Say "supervisor up (no relaunch ceiling; remedies escalate)"
$lastLeft = -1
$barren = 0        # consecutive relaunches that moved coverage by nothing
$launches = 0

while ($true) {
    $left = -1
    try { $left = [int](& $py bench/ui_missing_ids.py $idsFile $Preds --count) }
    catch { Say ("coverage unreadable: " + $_.Exception.Message); Start-Sleep -Seconds $CheckSec; continue }

    if ($left -eq 0) { Say "coverage complete; supervisor exiting"; break }
    if ($lastLeft -ge 0 -and $left -lt $lastLeft) {
        Say ("progress: {0} -> {1} uncovered" -f $lastLeft, $left)
        $barren = 0
    }
    $lastLeft = $left

    # Match on the driver's script name, not on "powershell.exe": every process here is
    # powershell.exe and matching the name finds this supervisor.
    $driver = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
                Where-Object { $_.CommandLine -like "*run_swe_via_ui.ps1*" })
    if ($driver.Count -gt 0) {
        Say ("driver alive (pid {0}), {1} uncovered" -f $driver[0].ProcessId, $left)
        Start-Sleep -Seconds $CheckSec
        continue
    }

    # THE DRIVER IS GONE AND WORK REMAINS. Fix the blocker before starting anything.
    if (-not (Test-Composer)) {
        $barren++
        Say ("composer unreachable (barren streak {0}); repairing before any relaunch" -f $barren)
        Repair-Cockpit ([math]::Min($barren, 2))
        if (-not (Test-Composer)) {
            # Do not spend a driver launch on a cockpit that still cannot take a goal.
            $wait = [math]::Min(600, $CheckSec * [math]::Max(1, $barren))
            Say ("composer still unreachable after repair; holding {0}s and trying again" -f $wait)
            Start-Sleep -Seconds $wait
            continue
        }
        Say "composer reachable again"
    }

    $launches++
    Say ("relaunching driver (launch {0}), {1} uncovered" -f $launches, $left)
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",".\scripts\win\run_swe_via_ui.ps1","-StartBatch","2","-MaxRounds","3" `
        -RedirectStandardOutput ".fleet\swe\ui_driver_sup$launches.log" `
        -RedirectStandardError  ".fleet\swe\ui_driver_sup$launches.err" `
        -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds $CheckSec
}
