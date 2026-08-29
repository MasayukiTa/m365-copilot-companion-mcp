# Drive a whole SWE-bench slice through the COCKPIT, start to finish, without stopping.
#
# WHY THROUGH THE UI. A run driven straight into the fleet proves the fleet works; it does not
# prove the cockpit hands it the same thing, and the gap between those two has bitten here --
# the back end was correct while the surface was full of errors, and "the tests pass" was true
# of a path nobody uses. submit_via_ui.ps1 says the same in its own header.
#
# WHY ONE SCRIPT FOR THE WHOLE SLICE. The previous plan staged one batch, submitted it, and
# reported -- which makes a stopping point per batch, and five batches means five chances to
# stop. This runs every batch to completion on its own.
#
# WHAT IT DOES PER BATCH: stage worktrees -> serialise the goals to one JSON line each (the
# cockpit splits its input on newlines, so a multi-line problem statement would otherwise
# become dozens of fragments) -> submit through the cockpit -> wait for the run to go idle ->
# capture the diffs.

[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice40_fresh.json",
    [int]$BatchSize = 8,
    [int]$PerBatchTimeoutSec = 3600,
    [string]$Preds = ".fleet/swe/ui_preds.json",
    [string]$Log = ".fleet/swe/ui_run.log"
)

$ErrorActionPreference = "Stop"
# The repository is found from this script's own location, not written in: a path in a
# tracked file names the machine it was written on.
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format HH:mm:ss), $m
    $line | Tee-Object -FilePath $Log -Append | Out-Host
}

# A FRESH ACCUMULATOR. Keeping the old one lets an interrupted run's patches be graded as this
# run's, which is the shortest path from this harness to a number nobody produced.
if (Test-Path $Preds) { Move-Item -LiteralPath $Preds -Destination "$Preds.prev" -Force }
"[]" | Set-Content -LiteralPath $Preds -Encoding utf8
if (Test-Path ".fleet/swe/pro_wt_map.json") { Remove-Item ".fleet/swe/pro_wt_map.json" -Force }
if (Test-Path ".fleet/swe/work") { Remove-Item ".fleet/swe/work" -Recurse -Force -ErrorAction SilentlyContinue }
Remove-Item $Log -Force -ErrorAction SilentlyContinue

# FAN-OUT MUST BE OFF, AND THE SCRIPT MAKES SURE RATHER THAN ASSUMING.
#
# The cockpit keeps `fanout` in its settings file, so a run inherits whatever the last person
# left it as. Measured: it was on, and eight SWE instances became FORTY-EIGHT workers -- each
# instance split into subtasks that then edit THE SAME worktree, which is the one arrangement
# the split job explicitly forbids. Every parent ended FANOUT, so the capture step would have
# read whatever several children had done to one checkout and called it that instance's patch.
#
# A SWE instance is one bug fix. It is not a divisible campaign, and no setting a person left
# behind should be able to make it into one.
$settings = Join-Path $env:APPDATA "copilot-bridge\settings.txt"
if (Test-Path $settings) {
    $body = Get-Content -LiteralPath $settings -Encoding UTF8
    if ($body -match '^fanout=on') {
        Say "fanout was ON in the cockpit settings -- turning it off for this run"
        ($body -replace '^fanout=on', 'fanout=off') |
            Set-Content -LiteralPath $settings -Encoding UTF8
    }
} else {
    Say "WARNING: cockpit settings not found; cannot confirm fanout is off"
}

$ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); print('\n'.join(sorted(x['instance_id'] for x in r)))" $SliceFile
$ids = @($ids -split "`n" | Where-Object { $_.Trim() })
Say ("START ui-run: {0} instances, batch={1}, slice={2}" -f $ids.Count, $BatchSize, $SliceFile)

$batches = [math]::Ceiling($ids.Count / $BatchSize)
for ($b = 0; $b -lt $batches; $b++) {
    $slice = $ids[($b * $BatchSize)..([math]::Min(($b + 1) * $BatchSize, $ids.Count) - 1)]
    Say ("=== batch {0}/{1}: {2} instances ===" -f ($b + 1), $batches, $slice.Count)

    $env:SWE_SLICE_FILE = $SliceFile
    $goalsFile = ".fleet/swe/ui_batch.jsonl"
    & $py bench/pro_stage_goals.py --ids ($slice -join ",") --out $goalsFile 2>&1 |
        Select-Object -Last 1 | ForEach-Object { Say ("stage: " + $_) }

    $uiFile = ".fleet/swe/ui_batch_lines.txt"
    $n = & $py -c "import sys; sys.path.insert(0,'.'); from bench.ui_goal_lines import write_ui_file; print(write_ui_file(sys.argv[1], sys.argv[2]))" $goalsFile $uiFile
    Say ("ui lines: {0}" -f $n)

    # SUBMIT THROUGH THE COCKPIT.
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\submit_via_ui.ps1 -GoalFile $uiFile 2>&1 |
            Select-Object -Last 3 | ForEach-Object { Say ("submit: " + $_) }
    } catch {
        Say ("submit FAILED: " + $_.Exception.Message)
    }

    # WAIT FOR IDLE. status.json's `running` is the cockpit's own view of the fleet, which is
    # the thing that was actually started -- polling the process list would answer a different
    # question and answer it wrong when several runs exist.
    $t0 = Get-Date
    $sawRunning = $false
    while (((Get-Date) - $t0).TotalSeconds -lt $PerBatchTimeoutSec) {
        Start-Sleep -Seconds 15
        $running = & $py -c "import json,io,os; p='.fleet/status.json'; d=json.load(io.open(p,encoding='utf-8')) if os.path.exists(p) else {}; print('1' if d.get('running') else '0')"
        if ($running -eq "1") { $sawRunning = $true }
        elseif ($sawRunning) { Say "batch fleet went idle"; break }
    }
    if (-not $sawRunning) { Say "WARNING: the fleet never reported running for this batch" }

    & $py bench/pro_capture.py --preds $Preds 2>&1 |
        Select-Object -Last 2 | ForEach-Object { Say ("capture: " + $_) }
}

$total = & $py -c "import json,io,sys; print(len(json.load(io.open(sys.argv[1],encoding='utf-8'))))" $Preds
Say ("DONE ui-run: {0} predictions -> {1}" -f $total, $Preds)
Say "UI_RUN_COMPLETE"
