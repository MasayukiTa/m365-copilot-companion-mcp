# N independent solves of the SAME instances at the SAME effort.
#
# WHY THIS AND NOT THE EFFORT A/B. Best-of-N only adds value where the candidates DISAGREE,
# and the effort A/B produced three arms that agreed on all six instances -- same five
# resolved, same one failed. The selector was handed six unanimous sets and never had to
# choose, so its accuracy over that data says nothing about it: measured, tested-on = 0.
#
# Disagreement between independent samples of ONE policy is the thing best-of-N exploits, so
# the arms here are identical by construction and only the sampling differs. Anything that
# differs besides the sample would make a disagreement mean something other than variance.
[CmdletBinding()]
param(
    [string]$SliceFile = ".fleet/swe/pro_slice50_full.json",
    [int]$Instances = 6,
    [int]$Skip = 6,                 # the A/B already burned the first six
    [int]$N = 3,
    [string]$Effort = "auto",
    [string]$OutDir = ".fleet/swe/bon",
    [int]$PerBatchTimeoutSec = 3600
    ,
    # AN EXPLICIT ID LIST BEATS AN OFFSET WHEN THE POPULATION MATTERS.
    #
    # The first run took the next six by slice position and drew six ansible instances
    # that every sample solved: three samples, six patches each, all byte-different and
    # all CORRECT. The selector again had nothing to choose between -- not because the
    # candidates matched but because none of them was wrong.
    #
    # Byte-difference is not the condition best-of-N exploits; difference in CORRECTNESS
    # is. So the caller must be able to name the instances rather than take whatever the
    # slice order gives.
    [string]$IdsFile = ""
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"
$settings = Join-Path $env:APPDATA "copilot-bridge\settings.txt"
$log = "$OutDir/bon_run.log"
New-Item -ItemType Directory -Force $OutDir | Out-Null

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

if (Test-Path $settings) {
    $body = Get-Content $settings -Raw
    if ($body -match '(?m)^effort=') { $body = $body -replace '(?m)^effort=.*', ("effort=" + $Effort) }
    else { $body = $body.TrimEnd() + "`neffort=" + $Effort + "`n" }
    [System.IO.File]::WriteAllText($settings, $body, (New-Object System.Text.UTF8Encoding($false)))
    Say ("effort fixed at: " + ((Get-Content $settings | Where-Object { $_ -like 'effort=*' })))
}

# INSTANCES AFTER THE ONES THE A/B USED. Reusing them would measure the selector on problems
# whose answers are already in this run's history.
if ($IdsFile -and (Test-Path $IdsFile)) {
    $ids = Get-Content $IdsFile | Where-Object { $_.Trim() }
    Say ("instances taken from {0}" -f $IdsFile)
} else {
    $ids = & $py -c "import json,io,sys; r=json.load(io.open(sys.argv[1],encoding='utf-8')); s=sorted(x['instance_id'] for x in r); print('\n'.join(s[int(sys.argv[2]):int(sys.argv[2])+int(sys.argv[3])]))" $SliceFile $Skip $Instances
}
$ids = @($ids -split "`n" | Where-Object { $_.Trim() })
Say ("best-of-N start: {0} instances x {1} samples at effort={2}" -f $ids.Count, $N, $Effort)
Set-Content -Path "$OutDir/bon_ids.txt" -Value ($ids -join "`n") -Encoding ascii

for ($k = 1; $k -le $N; $k++) {
    Say ("=== sample {0}/{1} ===" -f $k, $N)
    $preds = "$OutDir/preds_$k.json"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $preds), "[]", (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path ".fleet/swe/work") { cmd /c "rmdir /s /q `"$repo\.fleet\swe\work`"" 2>$null }
    if (Test-Path ".fleet/swe/pro_wt_map.json") { Remove-Item ".fleet/swe/pro_wt_map.json" -Force }

    $env:SWE_SLICE_FILE = $SliceFile
    & $py bench/pro_stage_goals.py --ids ($ids -join ",") --out "$OutDir/goals_$k.jsonl" 2>&1 |
        Select-Object -Last 1 | ForEach-Object { Say ("stage: " + $_) }
    # $lineCount, NOT $n. POWERSHELL VARIABLE NAMES ARE CASE-INSENSITIVE, so `$n` IS `$N` --
    # the sample count. Assigning the UI line count to it silently changed the loop bound from
    # three to six mid-run, and the log said "sample 1/3" then "sample 2/6".
    $lineCount = & $py -c "import sys; sys.path.insert(0,'.'); from bench.ui_goal_lines import write_ui_file; print(write_ui_file(sys.argv[1], sys.argv[2]))" "$OutDir/goals_$k.jsonl" "$OutDir/lines_$k.txt"
    Say ("ui lines: {0}" -f $lineCount)

    $submitted = $false
    for ($try = 1; $try -le 3 -and -not $submitted; $try++) {
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\submit_via_ui.ps1 -GoalFile "$OutDir/lines_$k.txt" 2>&1 |
                Select-Object -Last 4 | ForEach-Object { Say ("submit: " + $_) }
            $submitted = $true
        } catch { Say ("submit {0}/3 failed: {1}" -f $try, $_.Exception.Message); Start-Sleep -Seconds 20 }
    }
    if (-not $submitted) { Say ("sample {0} never submitted; skipping" -f $k); continue }

    $t0 = Get-Date; $sawRunning = $false
    while (((Get-Date) - $t0).TotalSeconds -lt $PerBatchTimeoutSec) {
        Start-Sleep -Seconds 15
        $running = & $py -c "import json,io,os; p='.fleet/status.json'; d=json.load(io.open(p,encoding='utf-8')) if os.path.exists(p) else {}; print('1' if d.get('running') else '0')"
        if ($running -eq "1") { $sawRunning = $true } elseif ($sawRunning) { Say "sample went idle"; break }
    }
    if (-not $sawRunning) { Say ("sample {0} never started; not capturing" -f $k); continue }

    & $py bench/pro_capture.py --preds $preds 2>&1 | Select-Object -Last 2 | ForEach-Object { Say ("capture: " + $_) }

    # BETWEEN SAMPLES, WHEN NOTHING IS BUILDING. The Go module cache reaches ~2.9 GB on a box
    # with single-digit GB free, and the hard population here includes Go projects. Clearing
    # it while a build is running turns a disk problem into a spurious FAILED instance, which
    # would then be indistinguishable from the model getting it wrong -- so it happens here,
    # after the capture and before the next sample starts.
    $freeGb = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
    if ($freeGb -lt 4.0) {
        $goExe = "C:\Program Files\Goin\go.exe"
        if (Test-Path $goExe) {
            Say ("free {0} GB; clearing the Go caches between samples" -f $freeGb)
            & $goExe clean -modcache 2>&1 | Select-Object -Last 1 | ForEach-Object { Say ("go clean: " + $_) }
            & $goExe clean -cache 2>&1 | Select-Object -Last 1 | ForEach-Object { Say ("go clean: " + $_) }
            Say ("free now {0} GB" -f [math]::Round((Get-PSDrive C).Free / 1GB, 2))
        }
    }
    & $py -c "import json,io,sys,time; p=sys.argv[1]; k=sys.argv[2]; rows=json.load(io.open(p,encoding='utf-8-sig')); io.open(p,'w',encoding='utf-8',newline='\n').write(json.dumps({'sample':int(k),'effort':sys.argv[3],'ts':time.time(),'predictions':rows},ensure_ascii=False))" $preds $k $Effort
    Say ("sample {0} recorded to {1}" -f $k, $preds)
}
Say "best-of-N sampling complete"
