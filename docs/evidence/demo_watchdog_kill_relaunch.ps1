# demo_watchdog_kill_relaunch.ps1
# Reproducible, SAFE demonstration that scripts/supervisor_watchdog.ps1 relaunches start_all
# after the supervisor process is gone, and WAITS while it is alive. Timestamped (UTC).
#
# SAFETY: this never touches the live supervisor, the live heartbeat, the real start_all, or
# any devtunnel. It starts a THROWAWAY powershell process as a stand-in "supervisor", writes a
# heartbeat into an isolated temp sandbox, and points the watchdog at that sandbox via its
# -HeartbeatPath / -StartAll parameters. The stand-in start_all only stamps a proof file.
[CmdletBinding()]
param([string]$SandboxRoot = (Join-Path $env:TEMP 'wd_demo_repro'))

$ErrorActionPreference='Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent  # docs/evidence -> repo root
$wd = Join-Path $repo 'scripts\supervisor_watchdog.ps1'
if (-not (Test-Path $wd)) { throw "watchdog not found: $wd" }

New-Item -ItemType Directory -Force -Path (Join-Path $SandboxRoot 'scripts') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $SandboxRoot '.fleet') | Out-Null
$startAll = Join-Path $SandboxRoot 'scripts\start_all.ps1'
$hb = Join-Path $SandboxRoot '.fleet\supervisor_heartbeat.json'
$proof = Join-Path $SandboxRoot '.fleet\relaunch_proof.txt'
Remove-Item $proof -ErrorAction SilentlyContinue
@'
param([switch]$CoreOnly,[switch]$NoSplash)
$ts=(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
Add-Content -LiteralPath (Join-Path $PSScriptRoot "..\.fleet\relaunch_proof.txt") -Value ("RELAUNCHED start_all CoreOnly=$CoreOnly NoSplash=$NoSplash at $ts pid=$PID")
'@ | Set-Content -LiteralPath $startAll -Encoding ascii

function U(){ (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ') }
function Log($m){ Write-Output ("[{0}] {1}" -f (U), $m) }
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# PHASE 1: stand-in supervisor (throwaway powershell; the live supervisor is untouched)
$dummy = Start-Process -FilePath $psExe -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 600' -PassThru -WindowStyle Hidden
$dpid = $dummy.Id
Log "PHASE1 stand-in supervisor pid=$dpid started (live supervisor untouched)"
$lt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
(@{pid=$dpid; local_time=$lt; free_ram_mb=625; cpu_pct=12} | ConvertTo-Json -Compress) | Set-Content -LiteralPath $hb -Encoding ascii
Log "PHASE1 fresh heartbeat local_time='$lt' pid=$dpid"

# PHASE 2: supervisor ALIVE -> watchdog must WAIT
$o1 = & $psExe -NoProfile -ExecutionPolicy Bypass -File $wd -HeartbeatPath $hb -StartAll $startAll -Once -Json 2>&1
Log "PHASE2 ALIVE -> $o1"

# PHASE 3: kill the stand-in supervisor (never the live one; nothing devtunnel)
Stop-Process -Id $dpid -Force; Start-Sleep -Milliseconds 400
$still = [bool](Get-Process -Id $dpid -ErrorAction SilentlyContinue)
Log "PHASE3 killed stand-in pid=$dpid stillAlive=$still"

# PHASE 4: age heartbeat beyond stale threshold (pid now dead)
$old = (Get-Date).AddSeconds(-600).ToString('yyyy-MM-dd HH:mm:ss')
(@{pid=$dpid; local_time=$old; free_ram_mb=625; cpu_pct=96} | ConvertTo-Json -Compress) | Set-Content -LiteralPath $hb -Encoding ascii
Log "PHASE4 heartbeat aged to local_time='$old'"

# PHASE 5: supervisor GONE + stale -> watchdog RELAUNCHES start_all
$o2 = & $psExe -NoProfile -ExecutionPolicy Bypass -File $wd -HeartbeatPath $hb -StartAll $startAll -Once -Json 2>&1
Log "PHASE5 GONE+STALE -> $o2"
$deadline=(Get-Date).AddSeconds(10)
while (-not (Test-Path $proof) -and (Get-Date) -lt $deadline){ Start-Sleep -Milliseconds 300 }
if (Test-Path $proof){ foreach($l in Get-Content $proof){ Log "PHASE5 proof: $l" } } else { Log 'PHASE5 proof MISSING' }
Log 'DEMO COMPLETE'
