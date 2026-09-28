param(
    [string]$OutputDir = (Join-Path $env:USERPROFILE 'Downloads')
)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $env:TEMP ("edge-respawn-diag-" + $stamp)
$zip = Join-Path $OutputDir ("edge-respawn-diag-" + $stamp + '.zip')
New-Item -ItemType Directory -Force $work | Out-Null

function Write-Text([string]$name, [string[]]$lines) {
    $p = Join-Path $work $name
    $lines | Set-Content -Path $p -Encoding UTF8
}

function Copy-IfPresent([string]$src, [string]$destName) {
    if (Test-Path $src) { Copy-Item $src (Join-Path $work $destName) -Force }
}

Push-Location $root
try {
    $meta = @()
    $meta += 'collected_at=' + (Get-Date).ToString('o')
    $meta += 'computer=' + $env:COMPUTERNAME
    $meta += 'user=' + $env:USERNAME
    $meta += 'repo=' + $root
    $head = (& git rev-parse HEAD 2>$null | Select-Object -First 1)
    $branch = (& git branch --show-current 2>$null | Select-Object -First 1)
    $meta += 'git_head=' + $head
    $meta += 'git_branch=' + $branch
    & git merge-base --is-ancestor 61409d7 HEAD 2>$null
    $meta += 'contains_signin_loop_fix_61409d7=' + ($(if ($LASTEXITCODE -eq 0) { 'yes' } else { 'no' }))
    $meta += 'git_status_begin'
    $meta += @(& git status --short 2>$null)
    $meta += 'git_status_end'
    foreach($n in 'MCP_CDP_WATCHDOG_SEC','MCP_CDP_WATCHDOG_FAILURES','MCP_PAGE_WEDGE_LIMIT_SEC','MCP_PAGE_STARTUP_LIMIT_SEC','MCP_FORCE_REHIDE_SEC','MCP_BRIDGE_CDP_PORT','MCP_BRIDGE_PORT','MCP_CONN_DEAD_GRACE_SEC','MCP_CONN_RECONNECT_TRIES') {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ([string]::IsNullOrWhiteSpace($v)) { $v = '<unset>' }
        $meta += $n + '=' + $v
    }
    Write-Text 'meta.txt' $meta

    $all = @(Get-CimInstance Win32_Process)
    $procLines = @()
    foreach($p in ($all | Where-Object { $_.Name -ieq 'msedge.exe' -and $_.CommandLine -and $_.CommandLine -notmatch '--type=' } | Sort-Object CreationDate)) {
        $profile = if ($p.CommandLine -match 'copilot-bridge-edge') { 'bridge' } elseif ($p.CommandLine -match 'copilot-companion-edge') { 'companion' } elseif ($p.CommandLine -match 'copilot-eval-edge') { 'eval' } else { 'other' }
        $procLines += ('EDGE pid={0} ppid={1} created={2} profile={3} cmd={4}' -f $p.ProcessId,$p.ParentProcessId,$p.CreationDate,$profile,$p.CommandLine)
    }
    foreach($p in ($all | Where-Object { $_.CommandLine -match 'start_bridge\.ps1|copilot_bridge\.py|supervisor\.ps1' } | Sort-Object CreationDate)) {
        $procLines += ('OWNER pid={0} ppid={1} created={2} name={3} cmd={4}' -f $p.ProcessId,$p.ParentProcessId,$p.CreationDate,$p.Name,$p.CommandLine)
    }
    Write-Text 'processes.txt' $procLines

    $portLines = @()
    foreach($port in 8765,9222,9223) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) { $portLines += ('port {0}: no listener' -f $port); continue }
        foreach($l in $listeners) {
            $p = $all | Where-Object ProcessId -eq $l.OwningProcess | Select-Object -First 1
            $portLines += ('port {0}: pid={1} name={2} created={3} cmd={4}' -f $port,$l.OwningProcess,$p.Name,$p.CreationDate,$p.CommandLine)
        }
    }
    Write-Text 'ports.txt' $portLines

    $logDir = Join-Path $root '.setup\logs'
    $fleet = Join-Path $root '.fleet'
    Copy-IfPresent (Join-Path $logDir 'bridge.log') 'bridge.log'
    Copy-IfPresent (Join-Path $logDir 'bridge.log.err') 'bridge.log.err'
    Copy-IfPresent (Join-Path $logDir 'companion_edge_copilot-bridge-edge.err.log') 'companion_edge_copilot-bridge-edge.err.log'
    Copy-IfPresent (Join-Path $logDir 'start_all_runs.jsonl') 'start_all_runs.jsonl'
    Copy-IfPresent (Join-Path $fleet 'signin_surfaced_9223.json') 'signin_surfaced_9223.json'
    Copy-IfPresent (Join-Path $fleet 'edge_mode_copilot-bridge-edge') 'edge_mode_copilot-bridge-edge'
    Copy-IfPresent (Join-Path $env:TEMP 'm365-companion-supervisor.log') 'supervisor.log'

    $pageCounts = Join-Path $fleet 'page_counts.jsonl'
    if (Test-Path $pageCounts) { Get-Content $pageCounts -Tail 1000 | Set-Content (Join-Path $work 'page_counts_tail.jsonl') -Encoding UTF8 }

    $bridgeLog = Join-Path $logDir 'bridge.log'
    if (Test-Path $bridgeLog) {
        $patterns = 'Launching dedicated companion Edge|HardReset|Sign-in required|HEADLESS; -Foreground requested|force-rehide|exiting for keepalive recovery|page-owner thread.*wedged|page-owner thread.*did not answer|CDP watchdog|browser connection.*gone|Sign-in complete'
        Select-String -Path $bridgeLog -Pattern $patterns | ForEach-Object { $_.Line } | Set-Content (Join-Path $work 'bridge_events.txt') -Encoding UTF8
    }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force $OutputDir | Out-Null
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $work '*') -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $work -Recurse -Force
Write-Output $zip