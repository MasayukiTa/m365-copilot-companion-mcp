# Grade both attempts of the retry experiment and compute rescue / regression.
#
# ONE COMMAND, because the measurement it finishes has been blocked three times by something
# other than the measurement: only the final patch was kept (fixed), the lens was the wrong
# one (fixed), and now the tunnel to the eval host needs an interactive re-authentication that
# a script cannot perform. Everything on this side is already staged, so when the tunnel is
# back this runs and the answer arrives.
#
# WHAT IT ANSWERS. Two forced attempts on the same six instances produced twelve patches, five
# of them differing between attempts. Grading both gives, for the first time, rescue
# (wrong at 1, correct at 2) and regression (correct at 1, wrong at 2). The completion floor
# cannot see the difference between those two, because both attempts report DONE.
[CmdletBinding()]
param(
    [string]$Dir = ".fleet/swe/retry_exp",
    [string]$Remote = "C:/swe-grade/retryexp",
    [string]$Log = ".fleet/swe/retry_exp/grade.log"
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

Say "checking the tunnel"
$probe = & ssh -o BatchMode=yes -o ConnectTimeout=20 $EvalHost "echo up" 2>&1
if ($probe -notmatch "up") {
    Say "the eval host is not reachable. Re-authenticate the tunnel, then run this again."
    exit 1
}
Say "tunnel is up"

$remoteWin = $Remote -replace '/', ''
& ssh -o BatchMode=yes $EvalHost ("powershell -NoProfile -Command New-Item -ItemType Directory -Force " + $remoteWin) 2>&1 | Out-Null
foreach ($f in @("bon_raw.jsonl", "grade_preds_1.json", "grade_preds_2.json")) {
    & scp -o BatchMode=yes "$Dir/$f" ("$($EvalHost):" + $Remote + "/") 2>&1 | Out-Null
}
& $py -c "import io; s=io.open('bench/remote/bon_grade.sh',encoding='utf-8').read().replace('/mnt/c/swe-grade/bon','/mnt/c/swe-grade/retryexp'); io.open('.fleet/swe/retryexp_grade.sh','w',encoding='utf-8',newline='
').write(s)"
& scp -o BatchMode=yes ".fleet/swe/retryexp_grade.sh" ("$($EvalHost):" + $Remote + "/bon_grade.sh") 2>&1 | Out-Null
Say "inputs shipped"

$tr = '"C:\Windows\System32\wsl.exe" -d Ubuntu -e /bin/bash /mnt/c/swe-grade/retryexp/bon_grade.sh'
& ssh -o BatchMode=yes $EvalHost "schtasks /Delete /TN RetryExpGrade /F" 2>&1 | Out-Null
& ssh -o BatchMode=yes $EvalHost ('schtasks /Create /TN RetryExpGrade /TR "' + $tr.Replace('"','\"') + '" /SC ONCE /ST 23:55 /RL HIGHEST /F') 2>&1 | ForEach-Object { Say ("remote: " + $_) }
& ssh -o BatchMode=yes $EvalHost "schtasks /Run /TN RetryExpGrade" 2>&1 | ForEach-Object { Say ("remote: " + $_) }

Say "waiting for the verdicts"
for ($i = 0; $i -lt 120; $i++) {
    $p = & ssh -o BatchMode=yes $EvalHost ("if exist " + $remoteWin + "on_grade.out (findstr /C:DONE_BON_GRADE " + $remoteWin + "on_grade.out) else (echo waiting)") 2>&1
    if ($p -match "DONE_BON_GRADE") { Say "grading finished"; break }
    Start-Sleep -Seconds 30
}
foreach ($k in @("1", "2")) {
    & scp -o BatchMode=yes ("$($EvalHost):" + $Remote + "/out_$k/eval_results.json") "$Dir/verdict_$k.json" 2>&1 | Out-Null
}
Say "verdicts fetched; computing rescue and regression"
& $py bench/retry_experiment_report.py --dir $Dir 2>&1 | ForEach-Object { Say $_ }
Say "done"
