# start_brain_path.ps1
# Bring up the LOCAL half of the Minecraft-bot brain path, detached + windowless:
#
#   :8011  relay.openai_endpoint_server  (OpenAI-compatible, Copilot-backed via CDP :9222)
#   :8012  relay\brain_proxy.py          (injects Authorization: Bearer <MCP_API_KEY>)
#
# The reverse tunnel to kiyus is a SEPARATE step: scripts\start_brain_tunnel.ps1.
#
# Idempotent: a port that is already listening is left alone. Requires the dedicated
# companion Edge to be up on CDP :9222 (start_companion_edge.ps1) -- this script does
# NOT touch, restart, or kill that Edge.
#
# AgentUrl note: the endpoint's default (.env MCP_IMPL_AGENT_URL) points at the
# desktopfile custom agent, which declines general questions. The brain path wants
# the DEFAULT Copilot chat, so we override MCP_IMPL_AGENT_URL per-process to the
# plain /chat/ URL (load_dotenv(override=False) respects the inherited env). An
# EMPTY value would NOT work: _find_agent_page then only reuses an already-open
# /chat/agent/ tab and 503s otherwise.
#
# Usage:
#   .\start_brain_path.ps1                       # start whatever is not yet up
#   .\start_brain_path.ps1 -AgentUrl <url>       # back the endpoint with another agent

param(
    [int]$EndpointPort = 8011,
    [int]$ProxyPort    = 8012,
    [string]$AgentUrl  = "https://m365.cloud.microsoft/chat/"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv python not found: $py"; exit 1 }

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Test-PortListening([int]$p) {
    $null -ne (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

function Wait-PortListening([int]$p, [int]$seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-PortListening $p) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# --- sanity: the companion Edge must already be answering CDP on :9222 ---
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:9222/json/version" | Out-Null
} catch {
    Write-Warning "[brain-path] CDP :9222 is not answering. Start the companion Edge first (start_companion_edge.ps1); completions will 503 until it is up."
}

$pids = @{}

# --- :8011 OpenAI-compatible endpoint ---
if (Test-PortListening $EndpointPort) {
    Write-Host "[brain-path] :$EndpointPort already listening -- leaving it alone"
} else {
    # Child inherits these; restore afterwards so the calling shell stays clean.
    $savedCompat = $env:OPENAI_COMPAT;        $env:OPENAI_COMPAT = "1"
    $savedAgent  = $env:MCP_IMPL_AGENT_URL;   $env:MCP_IMPL_AGENT_URL = $AgentUrl
    $savedPort   = $env:OPENAI_ENDPOINT_PORT; $env:OPENAI_ENDPOINT_PORT = "$EndpointPort"
    try {
        $p = Start-Process $py -ArgumentList "-m","relay.openai_endpoint_server" `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir "openai_endpoint.out.log") `
            -RedirectStandardError  (Join-Path $logDir "openai_endpoint.err.log")
    } finally {
        $env:OPENAI_COMPAT = $savedCompat
        $env:MCP_IMPL_AGENT_URL = $savedAgent
        $env:OPENAI_ENDPOINT_PORT = $savedPort
    }
    if (-not (Wait-PortListening $EndpointPort 30)) {
        Write-Error "[brain-path] :$EndpointPort did not come up; see $logDir\openai_endpoint.err.log"
        exit 1
    }
    $pids["endpoint"] = $p.Id
    Write-Host "[brain-path] endpoint up on :$EndpointPort (PID $($p.Id), agent=$AgentUrl)"
}

# --- :8012 key-injecting brain proxy ---
if (Test-PortListening $ProxyPort) {
    Write-Host "[brain-path] :$ProxyPort already listening -- leaving it alone"
} else {
    $savedProxyPort = $env:BRAIN_PROXY_PORT; $env:BRAIN_PROXY_PORT = "$ProxyPort"
    $savedUpPort    = $env:BRAIN_UPSTREAM_PORT; $env:BRAIN_UPSTREAM_PORT = "$EndpointPort"
    try {
        $p = Start-Process $py -ArgumentList (Join-Path $root "relay\brain_proxy.py") `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir "brain_proxy.out.log") `
            -RedirectStandardError  (Join-Path $logDir "brain_proxy.err.log")
    } finally {
        $env:BRAIN_PROXY_PORT = $savedProxyPort
        $env:BRAIN_UPSTREAM_PORT = $savedUpPort
    }
    if (-not (Wait-PortListening $ProxyPort 15)) {
        Write-Error "[brain-path] :$ProxyPort did not come up; see $logDir\brain_proxy.err.log"
        exit 1
    }
    $pids["proxy"] = $p.Id
    Write-Host "[brain-path] brain_proxy up on :$ProxyPort (PID $($p.Id)) -> 127.0.0.1:$EndpointPort"
}

Write-Host "[brain-path] local chain ready: :$ProxyPort (key-inject) -> :$EndpointPort (Copilot). Next: scripts\start_brain_tunnel.ps1"
