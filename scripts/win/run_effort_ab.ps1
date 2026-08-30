# Three effort arms over the SAME instances, driven through the UI.
#
# WHAT THIS ANSWERS. relay/fleet_runner.py records the cost of uniform effort -- "a UNIFORM
# ultra over-engineers easy tasks (observed: 44-47 line diffs for 2-7 line gold fixes)" -- and
# that observation has never been checked through a scorecard, because until now the harness
# could not express an arm. It still cannot on the companionbench path: FleetAgent.__init__
# takes `refuter` and nothing else. The UI path can, because the cockpit persists effort= to
# settings.txt and the fleet runner reads it at launch.
#
# WHAT IT IS NOT. It is not a matched-BUDGET comparison, and calling it one would be the
# claim that ruins it. The arms get the same INSTANCES, not the same number of turns, so ultra
# may win by spending more -- which is not a finding. Turns are recorded per arm so the cost
# is beside the rate and a budget-matched comparison can be derived afterwards. Saying which
# of the two this is matters more than the numbers.
#
# THE ARM IS RECORDED WITH THE RESULT. A run whose effort is not written down cannot be
# compared later; the same omission is why the earlier "ultra over-engineers" note has a
# number and no run behind it.
[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice50_full.json",
    [int]$Instances = 8,
    [string[]]$Arms = @("min", "auto", "ultra"),
    [string]$OutDir = ".fleet/swe/ab",
    [int]$PerBatchTimeoutSec = 3600
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"
$settings = Join-Path $env:APPDATA "copilot-bridge\settings.txt"
$log = "$OutDir/ab_run.log"
New-Item -ItemType Directory -Force $OutDir | Out-Null

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Set-Effort([string]$arm) {
    # Written, not checked-then-written: the setting carries whatever the last run or the last
    # person left, which is exactly how a batch of eight instances became forty-eight workers.
    if (-not (Test-Path $settings)) { Say "no settings file; cannot set the arm"; return $false }
    $body = Get-Content $settings -Raw
    if ($body -match '(?m)^effort=') { $body = $body -replace '(?m)^effort=.*', ("effort=" + $arm) }
    else { $body = $body.TrimEnd() + "`neffort=" + $arm + "`n" }
    [System.IO.File]::WriteAllText($settings, $body, (New-Object System.Text.UTF8Encoding($false)))
    $back = (Get-Content $settings | Where-Object { $_ -like 'effort=*' })
    Say ("arm set: " + $back)
    return ($back -eq ("effort=" + $arm))
}

# THE SAME INSTANCES FOR EVERY ARM, chosen once. Choosing per arm would compare arms on
# different problems and call the difference an effect.
$ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); print('\n'.join(sorted(x['instance_id'] for x in r)[:int(sys.argv[2])]))" $SliceFile $Instances
$ids = @($ids -split "`n" | Where-Object { $_.Trim() })
Say ("A/B start: {0} instances x {1} arms ({2}) from {3}" -f $ids.Count, $Arms.Count, ($Arms -join ","), $SliceFile)
Set-Content -Path "$OutDir/ab_ids.txt" -Value ($ids -join "`n") -Encoding ascii

foreach ($arm in $Arms) {
    Say ("=== arm {0} ===" -f $arm)
    if (-not (Set-Effort $arm)) { Say ("could not set arm {0}; skipping it rather than running it mislabelled" -f $arm); continue }

    $preds = "$OutDir/preds_$arm.json"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $preds), "[]",
                                   (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path ".fleet/swe/work") { cmd /c "rmdir /s /q `"$repo\.fleet\swe\work`"" 2>$null }
    if (Test-Path ".fleet/swe/pro_wt_map.json") { Remove-Item ".fleet/swe/pro_wt_map.json" -Force }

    $env:SWE_SLICE_FILE = $SliceFile
    & $py bench/pro_stage_goals.py --ids ($ids -join ",") --out "$OutDir/goals_$arm.jsonl" 2>&1 |
        Select-Object -Last 1 | ForEach-Object { Say ("stage: " + $_) }
    $n = & $py -c "import sys; sys.path.insert(0,'.'); from bench.ui_goal_lines import write_ui_file; print(write_ui_file(sys.argv[1], sys.argv[2]))" "$OutDir/goals_$arm.jsonl" "$OutDir/lines_$arm.txt"
    Say ("ui lines: {0}" -f $n)

    $submitted = $false
    for ($try = 1; $try -le 3 -and -not $submitted; $try++) {
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\submit_via_ui.ps1 -GoalFile "$OutDir/lines_$arm.txt" 2>&1 |
                Select-Object -Last 2 | ForEach-Object { Say ("submit: " + $_) }
            $submitted = $true
        } catch { Say ("submit {0}/3 failed: {1}" -f $try, $_.Exception.Message); Start-Sleep -Seconds 20 }
    }
    if (-not $submitted) { Say ("arm {0} was never submitted; not waiting and not capturing" -f $arm); continue }

    $t0 = Get-Date; $sawRunning = $false
    while (((Get-Date) - $t0).TotalSeconds -lt $PerBatchTimeoutSec) {
        Start-Sleep -Seconds 15
        $running = & $py -c "import json,io,os; p='.fleet/status.json'; d=json.load(io.open(p,encoding='utf-8')) if os.path.exists(p) else {}; print('1' if d.get('running') else '0')"
        if ($running -eq "1") { $sawRunning = $true } elseif ($sawRunning) { Say "arm went idle"; break }
    }
    if (-not $sawRunning) { Say ("arm {0} never started; not capturing" -f $arm); continue }

    & $py bench/pro_capture.py --preds $preds 2>&1 | Select-Object -Last 2 | ForEach-Object { Say ("capture: " + $_) }

    # THE ARM, WRITTEN NEXT TO THE RESULT. Without it the file is a set of patches with no
    # statement of what produced them.
    & $py -c "import json,io,sys,time; p=sys.argv[1]; a=sys.argv[2]; rows=json.load(io.open(p,encoding='utf-8-sig')); [r.update({'arm':a}) for r in rows]; io.open(p,'w',encoding='utf-8',newline='\n').write(json.dumps({'arm':a,'ts':time.time(),'predictions':rows},ensure_ascii=False))" $preds $arm
    Say ("arm {0} recorded to {1}" -f $arm, $preds)
}
Say "A/B complete"
