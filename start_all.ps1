# start_all.ps1 -- idempotent DAILY startup for the whole stack.
# Called by start_all.bat (double-click). Brings up, in order and ONLY IF NOT ALREADY RUNNING:
#   1. supervisor.ps1  (MCP server + devtunnel host)   -- mutex-guarded; the live tunnel is NEVER
#      killed, so re-running while a tunnel/supervisor is already up is a no-op.
#   2. the companion Edge :9222 (for the fleet / agent) -- skipped if its CDP port already answers.
#   3. start_bridge.ps1 -Keepalive (bridge :9223 + chat UI backend) -- skipped if already running.
#   4. the two WPF apps (CopilotChat, FleetCockpit)     -- launched only if not already running.
# Nothing is ever stopped/killed; this only fills in what is missing. Safe to run any number of times.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot

function Proc-Running([string]$pattern) {
    try {
        return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                      Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) })
    } catch { return $false }
}
function Port-Up([int]$p) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$p/json/version" | Out-Null; return $true }
    catch { return $false }
}
function Http-Up([string]$url) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $url | Out-Null; return $true } catch { return $false }
}

Write-Host "=== Daily startup (idempotent -- already-running parts are left as-is) ==="

# 1) Supervisor = MCP server + Dev Tunnel host. Its own global mutex makes a second instance exit
#    quietly, and it never touches a live `devtunnel host` -- so if a tunnel is already up, we keep
#    it. We still gate on the process so we don't spawn a doomed hidden window each run.
if (Proc-Running 'supervisor\.ps1') {
    Write-Host "[1/4] supervisor (MCP server + tunnel): already running -- left as-is"
} else {
    Write-Host "[1/4] supervisor (MCP server + tunnel): starting"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile","-ExecutionPolicy","Bypass","-File","$root\supervisor.ps1")
}

# 2) Companion Edge :9222 (the fleet / agent Edge). Idempotent launcher; skip if the port answers.
if (Port-Up 9222) {
    Write-Host "[2/4] companion Edge :9222: already up"
} else {
    Write-Host "[2/4] companion Edge :9222: starting (headless)"
    try { & "$root\start_companion_edge.ps1" -Headless | Out-Null } catch { Write-Host "      (companion Edge launch returned: $_)" }
}

# 3) Bridge :9223 + chat backend (start_bridge -Keepalive). Skip if the keepalive supervisor is up.
if (Proc-Running 'start_bridge\.ps1') {
    Write-Host "[3/4] bridge keepalive: already running"
} elseif (Http-Up "http://127.0.0.1:8765/conv") {
    Write-Host "[3/4] bridge :8765: already serving (no keepalive supervisor, but up)"
} else {
    Write-Host "[3/4] bridge: starting (headless keepalive)"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile","-ExecutionPolicy","Bypass","-File","$root\start_bridge.ps1","-Keepalive")
}

# 4) WPF apps. Launch only if not already running; build them first if the exe is missing.
foreach ($app in @("CopilotChat","FleetCockpit")) {
    if (Get-Process $app -ErrorAction SilentlyContinue) {
        Write-Host "[4/4] ${app}: already running"
    } elseif (Test-Path "$root\ui\$app.exe") {
        Write-Host "[4/4] ${app}: launching"
        Start-Process "$root\ui\$app.exe"
    } else {
        Write-Host "[4/4] $app.exe not built yet -- run  ui\rebuild_ui.ps1  once, then re-run this."
    }
}

Write-Host ""
Write-Host "Done. Chat UI: http://127.0.0.1:8765 (or the CopilotChat window). Fleet cockpit window is up."
Write-Host "If a one-time M365 sign-in is needed, a visible Edge window will appear -- sign in there."
