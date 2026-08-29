# WHY A SUPERVISOR EXISTS AT ALL.
#
# On 2026-08-29 the run driver printed "DONE ui-run: 40 predictions" at 19:17 and exited.
# Twenty-three of the forty instances had no patch. Nothing was running, nothing was
# watching, and the box sat idle for three hours until a person asked why.
#
# The driver was rewritten so it re-works uncovered instances instead of counting rows --
# but that only helps while the driver is alive. A driver that dies, is killed, or exits
# on an unhandled error still leaves the same silence. Restarting it is not a decision that
# needs judgement, so it should not need a person: the finish condition is coverage, and
# coverage is a file this can read.
#
# It does NOT start a second driver while one is running: two drivers submitting into one
# cockpit steer each other's goals, which is the collision this repository has already been
# bitten by once (two fleets on one state directory).
[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice40_fresh.json",
    [string]$Preds     = ".fleet/swe/ui_preds.json",
    [string]$Log       = ".fleet/swe/ui_supervisor.log",
    [int]$CheckSec     = 120,
    # A ceiling, so a driver that cannot make progress is not relaunched forever.
    [int]$MaxRelaunch  = 8
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

$idsFile = ".fleet/swe/ui_all_ids.txt"
if (-not (Test-Path $idsFile)) {
    $ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); print('\n'.join(sorted(x['instance_id'] for x in r)))" $SliceFile
    Set-Content -Path $idsFile -Value $ids -Encoding ascii
}

Say "supervisor up"
$relaunches = 0
while ($true) {
    $left = 0
    try {
        $left = [int](& $py bench/ui_missing_ids.py $idsFile $Preds --count)
    } catch {
        Say ("could not read coverage: " + $_.Exception.Message)
        Start-Sleep -Seconds $CheckSec
        continue
    }
    if ($left -eq 0) { Say "coverage complete; supervisor exiting"; break }

    # IS A DRIVER ALIVE? Ask for the command line, not for the process name: every
    # PowerShell on this box is powershell.exe, and matching on the name would find the
    # supervisor itself and conclude a driver was running.
    $driver = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
                Where-Object { $_.CommandLine -like "*run_swe_via_ui.ps1*" })
    if ($driver.Count -gt 0) {
        Say ("driver alive (pid {0}), {1} uncovered" -f $driver[0].ProcessId, $left)
        Start-Sleep -Seconds $CheckSec
        continue
    }

    if ($relaunches -ge $MaxRelaunch) {
        Say ("driver gone with {0} uncovered, but {1} relaunches already spent; stopping" -f $left, $relaunches)
        break
    }
    $relaunches++
    Say ("driver gone with {0} uncovered; relaunch {1}/{2}" -f $left, $relaunches, $MaxRelaunch)
    # -StartBatch 2 only because 1 is what clears the accumulator; the driver takes its
    # pending set from coverage either way.
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",".\scripts\win\run_swe_via_ui.ps1","-StartBatch","2","-MaxRounds","3" `
        -RedirectStandardOutput ".fleet\swe\ui_driver_sup$relaunches.log" `
        -RedirectStandardError  ".fleet\swe\ui_driver_sup$relaunches.err" `
        -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds $CheckSec
}
