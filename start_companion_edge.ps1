# start_companion_edge.ps1
# Launch a DEDICATED, ISOLATED Edge instance for the companion (bridge + fleet).
#
# Why a separate instance (not your everyday Edge):
#   1. --remote-debugging-port only binds on a FRESH Edge instance. A distinct
#      --user-data-dir guarantees a fresh instance, so the CDP port reliably opens.
#   2. It is fully isolated from your personal Edge. Your main browser can hold
#      dozens of heavy M365 tabs; they cannot starve the companion's RAM or take it
#      down, and the companion closing its tabs frees only its own memory. This is the
#      fix for the "many M365 tabs -> Edge crashes -> port 9222 disappears" failure.
#
# One-time: a window opens on M365 Copilot. Sign in with your work account once; the
# profile persists, so future launches are already signed in. The bridge and the
# parallel fleet both connect to http://127.0.0.1:<port>.
#
# Usage:
#   .\start_companion_edge.ps1                 # default port 9222
#   .\start_companion_edge.ps1 -Port 9333      # custom port

param(
    [int]$Port = $(if ($env:MCP_CDP_PORT) { [int]$env:MCP_CDP_PORT } else { 9222 }),
    [string]$Url = "https://m365.cloud.microsoft/chat"
)

$ErrorActionPreference = "Stop"

$dataDir = Join-Path $env:LOCALAPPDATA "copilot-companion-edge"

# Idempotent: if something is already listening on the port, assume the companion
# Edge is up and do nothing (avoid spawning a second instance / fighting for the port).
$listening = $false
try {
    $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} catch { }
if ($listening) {
    Write-Host "Companion Edge already reachable on port $Port (http://127.0.0.1:$Port). Nothing to do."
    exit 0
}

# Locate msedge.exe (standard install paths, then PATH).
$edge = $null
$candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
foreach ($c in $candidates) { if (Test-Path $c) { $edge = $c; break } }
if (-not $edge) {
    $cmd = Get-Command msedge.exe -ErrorAction SilentlyContinue
    if ($cmd) { $edge = $cmd.Source }
}
if (-not $edge) { throw "msedge.exe not found. Install Microsoft Edge or pass its path." }

$arguments = @(
    "--user-data-dir=$dataDir",
    "--remote-debugging-port=$Port",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-allow-origins=*",
    $Url
)

Write-Host "Launching dedicated companion Edge:"
Write-Host "  exe:     $edge"
Write-Host "  profile: $dataDir   (isolated from your main Edge)"
Write-Host "  port:    $Port"
Start-Process -FilePath $edge -ArgumentList $arguments | Out-Null

# Wait for the CDP endpoint to come up so the caller knows it is ready.
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    $up = $false
    try { $up = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) } catch { }
    if ($up) {
        Write-Host ""
        Write-Host "Ready: CDP endpoint is up on http://127.0.0.1:$Port"
        Write-Host "If this profile is new, sign in to M365 once in the window that opened."
        exit 0
    }
}
Write-Host "Edge launched, but the CDP port did not come up within 30s. Check the Edge window."
exit 1
