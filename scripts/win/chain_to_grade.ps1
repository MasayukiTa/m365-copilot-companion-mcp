# Wait for full coverage, then ship the predictions and start grading on the eval host.
#
# WHY THIS IS A WINDOWS PROCESS AND NOT A BACKGROUNDED SHELL JOB. The first version was
# `nohup bash chain.sh &` launched from a tool call; the wrapper exited and took the child
# with it, and the log held exactly one line -- "chain up; waiting for coverage" -- for as
# long as anyone cared to look. Start-Process survives the caller, which the run driver and
# the supervisor have both demonstrated in this session.
#
# It exists so that "the bench finished" and "grading started" are not two separate
# occasions for a person to have to notice something.
[CmdletBinding()]
param(
    [string]$Preds = ".fleet/swe/ui_preds.json",
    [string]$Ids   = ".fleet/swe/ui_all_ids.txt",
    [string]$Log   = ".fleet/swe/chain.log",
    [int]$CheckSec = 30,
    [int]$MaxWaitMin = 360
)

# The eval host's ssh alias is not written in this repository; it comes from the
# environment or from .env. See scripts/win/eval_host.ps1.
. "$PSScriptRoot\eval_host.ps1"
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say "chain up; waiting for coverage"
$deadline = (Get-Date).AddMinutes($MaxWaitMin)
$left = -1
$tick = 0
while ((Get-Date) -lt $deadline) {
    try { $left = [int](& $py bench/ui_missing_ids.py $Ids $Preds --count) } catch { $left = -1 }
    if ($left -eq 0) { Say "coverage complete"; break }
    $tick++
    if ($tick % 10 -eq 0) { Say ("still {0} uncovered" -f $left) }
    Start-Sleep -Seconds $CheckSec
}
if ($left -ne 0) { Say ("GIVING UP: still {0} uncovered after {1} minutes" -f $left, $MaxWaitMin); exit 1 }

Say "shipping predictions and the grading script"
& scp -o BatchMode=yes $Preds "$($EvalHost):C:/swe-grade/ui_20260829/" 2>&1 | ForEach-Object { Say ("scp: " + $_) }
& scp -o BatchMode=yes "bench/remote/ui_grade40.sh" "$($EvalHost):C:/swe-grade/ui_20260829/" 2>&1 | ForEach-Object { Say ("scp: " + $_) }

Say "starting grading on the eval host, detached"
$remote = 'C:\Windows\System32\wsl.exe -d Ubuntu -e bash -c "chmod +x /mnt/c/swe-grade/ui_20260829/ui_grade40.sh; setsid nohup bash /mnt/c/swe-grade/ui_20260829/ui_grade40.sh >/dev/null 2>&1 </dev/null & echo launched"'
& ssh -o BatchMode=yes $EvalHost $remote 2>&1 | ForEach-Object { Say ("remote: " + $_) }
Say "chain done. grading output: C:/swe-grade/ui_20260829/grade.out on the eval host"
