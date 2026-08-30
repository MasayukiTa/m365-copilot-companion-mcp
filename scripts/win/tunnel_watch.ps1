# Keep trying the eval host until it answers, then grade -- do not conclude it is down.
#
# TWO TIMEOUTS ARE NOT A DIAGNOSIS. A "banner exchange" timeout means the tunnel edge accepted
# the connection and no SSH greeting came back, which is consistent with a sleeping machine, a
# cold tunnel, a stale cached connection, an expired access token being renewed, or nothing at
# all. Concluding "the server is down" from two attempts stops the work on an assumption; this
# keeps asking and does the job the moment it can.
#
# Each probe is cheap and the interval is long, so this costs nothing while it waits.
[CmdletBinding()]
param(
    [int]$EverySec = 300,
    [int]$MaxHours = 24,
    [string]$Log = ".fleet/swe/tunnel_watch.log"
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say ("watching the eval host every {0}s; will grade the retry experiment when it answers" -f $EverySec)
$deadline = (Get-Date).AddHours($MaxHours)
$attempt = 0
while ((Get-Date) -lt $deadline) {
    $attempt++
    $r = & ssh -o BatchMode=yes -o ConnectTimeout=25 EVAL_HOST "echo up" 2>&1
    if ($r -match "up") {
        Say ("reachable on attempt {0}" -f $attempt)
        break
    }
    $why = ($r -join " ")
    if ($why.Length -gt 110) { $why = $why.Substring(0, 110) }
    Say ("attempt {0}: not yet -- {1}" -f $attempt, $why)
    Start-Sleep -Seconds $EverySec
}

$r = & ssh -o BatchMode=yes -o ConnectTimeout=25 EVAL_HOST "echo up" 2>&1
if ($r -notmatch "up") { Say "gave up after the window; the experiment is still staged and nothing was lost"; exit 1 }

Say "grading the retry experiment"
& powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\win\grade_retry_experiment.ps1 2>&1 |
    ForEach-Object { Say ("grade: " + $_) }
Say "tunnel watch done"
