# start_bridge.ps1
# Launch the interactive Copilot BRIDGE on its OWN dedicated Edge, fully isolated from the
# SWE fleet's Edge. This lets the bridge and a running fleet COEXIST:
#   * the fleet owns :9222 / profile copilot-companion-edge  (and hard-resets it per chunk)
#   * the bridge owns :9223 / profile copilot-bridge-edge    (the fleet never touches it)
# Edge locks a profile to a single process, so concurrent use REQUIRES distinct profiles
# + CDP ports -- which is exactly what this sets up.
#
# First run: a visible Edge window opens on M365 Copilot in the NEW bridge profile. If it is
# not already signed in via SSO, sign in ONCE with your work account; the profile persists,
# so later launches are already signed in. Then open the bridge UI in your browser.
#
# Usage:
#   .\start_bridge.ps1                          # Edge CDP :9223 (copilot-bridge-edge), UI :8765
#   .\start_bridge.ps1 -CdpPort 9224 -BridgePort 8766
#   .\start_bridge.ps1 -HardReset               # relaunch the bridge Edge clean

param(
    [int]$CdpPort     = $(if ($env:MCP_BRIDGE_CDP_PORT) { [int]$env:MCP_BRIDGE_CDP_PORT } else { 9223 }),
    [int]$BridgePort  = $(if ($env:MCP_BRIDGE_PORT)     { [int]$env:MCP_BRIDGE_PORT }     else { 8765 }),
    [string]$Profile  = "copilot-bridge-edge",
    [switch]$HardReset
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# 1) Bring up the dedicated bridge Edge (separate profile + port). Foreground/visible so a
#    one-time sign-in is possible. The launcher is idempotent: if :$CdpPort is already
#    listening it does nothing (won't fight for the port or spawn a second instance).
$edgeArgs = @("-Port", "$CdpPort", "-Profile", $Profile, "-Foreground")
if ($HardReset) { $edgeArgs += "-HardReset" }
& (Join-Path $root "start_companion_edge.ps1") @edgeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Bridge Edge did not come up on :$CdpPort. If a sign-in window is open, sign in and re-run."
    exit 1
}

# 2) Start the Python bridge, pointed at the bridge Edge's CDP port (NOT the fleet's :9222).
$env:MCP_CDP_URL     = "http://127.0.0.1:$CdpPort"
$env:MCP_BRIDGE_PORT = "$BridgePort"
$py = Join-Path $root ".venv\Scripts\python.exe"
Write-Host ""
Write-Host "Starting bridge:  UI http://127.0.0.1:$BridgePort   ->   Edge CDP :$CdpPort  (profile $Profile)"
Write-Host "(The fleet's :9222 Edge is untouched -- you can run a SWE fleet at the same time.)"
& $py (Join-Path $root "bridge\copilot_bridge.py")
