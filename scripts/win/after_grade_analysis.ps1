# Wait for the grader's verdict file, fetch it, and produce the analysis that needed an oracle.
#
# WHY IT IS A SEPARATE PROCESS. The grading takes hours on the eval host; the analysis takes
# seconds here. Chaining them means the moment the verdicts exist, the three things that have
# been waiting for an external check are computed without anybody noticing that grading ended:
#
#   calibration_report   resolved rate per task class, which is what effort routing routes ON
#   done_vs_correct      how often a worker said DONE and was wrong -- the number every
#                        self-reported metric in this repository has been blind to
#   swe_run_facts        the ledger joined to the graded slice
[CmdletBinding()]
param(
    [string]$Log = ".fleet/swe/after_grade.log",
    [int]$CheckSec = 120,
    [int]$MaxHours = 8
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"
$remoteOut = "C:/swe-grade/ui_20260829/out/eval_results.json"
$local = ".fleet/swe/eval_results.json"

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say "waiting for the grader's verdicts"
$deadline = (Get-Date).AddHours($MaxHours)
$got = $false
while ((Get-Date) -lt $deadline) {
    $probe = & ssh -o BatchMode=yes EVAL_HOST "if exist `"C:\swe-grade\ui_20260829\out\eval_results.json`" (echo READY) else (echo waiting)" 2>&1
    if ($probe -match "READY") { $got = $true; Say "verdict file exists"; break }
    Start-Sleep -Seconds $CheckSec
}
if (-not $got) { Say "GIVING UP: no verdict file after $MaxHours hours"; exit 1 }

& scp -o BatchMode=yes ("EVAL_HOST:" + $remoteOut) $local 2>&1 | ForEach-Object { Say ("scp: " + $_) }
if (-not (Test-Path $local)) { Say "the verdict file did not arrive"; exit 1 }

Say "--- calibration report (resolved rate per task class) ---"
& $py -m relay.selfimprove.calibration $local 2>&1 | ForEach-Object { Say $_ }

Say "--- how often DONE was right ---"
& $py bench/done_vs_correct.py --eval $local 2>&1 | ForEach-Object { Say $_ }

Say "analysis complete"
