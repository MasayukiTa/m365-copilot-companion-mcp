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

#   .\start_bridge.ps1 -Keepalive               # supervise: restart the bridge if it exits,
#                                                 re-bring-up the Edge if CDP :9223 drops
#
# Why -Keepalive: the bridge drives the cockpit's auto-delete + history-scrape. Run as a single
# foreground process it dies when the terminal closes or the bridge crashes, and then those ops
# silently fail ("自動削除はできませんでした" / "本文はまだ取得できません") because :9223 is simply
# DOWN. Keepalive holds :9223 + the bridge up across crashes (first SSO sign-in is still a
# one-time interactive step; after that the profile is signed in, so restarts are unattended).

param(
    [int]$CdpPort     = $(if ($env:MCP_BRIDGE_CDP_PORT) { [int]$env:MCP_BRIDGE_CDP_PORT } else { 9223 }),
    [int]$BridgePort  = $(if ($env:MCP_BRIDGE_PORT)     { [int]$env:MCP_BRIDGE_PORT }     else { 8765 }),
    [string]$Profile  = "copilot-bridge-edge",
    [switch]$HardReset,
    [switch]$Keepalive,
    [switch]$SignIn        # force a VISIBLE window now to sign in (normally automatic on demand)
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Bring up the dedicated bridge Edge (separate profile + port). HEADLESS by default -- no window,
# no taskbar flash, zero foreground interference; the profile's SSO persists so it just connects.
# A VISIBLE window is shown ONLY when interactive sign-in is actually required (-Visible). An
# already-authenticated launch must NEVER pop a Copilot window the user has to close -- that was
# the wrong trigger. Idempotent: if :$CdpPort already listens it does nothing.
function Ensure-Edge([switch]$Hard, [switch]$Visible) {
    # Hashtable splat (NOT array splat): array splatting an int-typed -Port mis-binds ("cannot
    # convert '-Port' to Int32"), which silently broke every supervisor launch -- the Edge only
    # ever came up when start_companion_edge.ps1 was run by hand. Hashtable splat binds by name.
    $p = @{ Port = $CdpPort; Profile = $Profile }
    if ($Visible) { $p["Foreground"] = $true } else { $p["Headless"] = $true }
    if ($Hard)    { $p["HardReset"]  = $true }
    & (Join-Path $root "start_companion_edge.ps1") @p
    return ($LASTEXITCODE -eq 0)
}

# True when the bridge Edge's CDP endpoint is actually answering on :$CdpPort.
function Test-Cdp {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$CdpPort/json/version" | Out-Null; return $true }
    catch { return $false }
}

# True ONLY when the bridge Edge is parked on a REAL interactive sign-in page. The CsrToSSR
# redirect (".../chat/?redirfrom=CsrToSSR&auth=2") auto-resolves for an authenticated profile and
# is NOT a sign-in wall, so it is deliberately excluded -- this is the only condition under which a
# window is surfaced.
function Needs-SignIn {
    try {
        $c = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$CdpPort/json").Content
        return ($c -match 'login\.microsoftonline\.com|login\.live\.com|/oauth2/|//login\.')
    } catch { return $false }
}

# Initial bring-up: headless unless the user explicitly asked to sign in.
if (-not (Ensure-Edge -Hard:$HardReset -Visible:$SignIn)) {
    Write-Host "Bridge Edge did not come up on :$CdpPort."
    exit 1
}

# Start the Python bridge, pointed at the bridge Edge's CDP port (NOT the fleet's :9222).
$env:MCP_CDP_URL     = "http://127.0.0.1:$CdpPort"
$env:MCP_BRIDGE_PORT = "$BridgePort"
$py     = Join-Path $root ".venv\Scripts\python.exe"
$bridge = Join-Path $root "bridge\copilot_bridge.py"
Write-Host ""
Write-Host "Starting bridge (headless):  UI http://127.0.0.1:$BridgePort   ->   Edge CDP :$CdpPort  (profile $Profile)"
Write-Host "(The fleet's :9222 Edge is untouched -- you can run a SWE fleet at the same time.)"

if (-not $Keepalive) {
    & $py $bridge
    # If the bridge could not reach the agent because a real sign-in wall is up, surface a window.
    if (Needs-SignIn) { Write-Host "Sign-in required -> opening a visible window once."; Ensure-Edge -Hard -Visible | Out-Null }
    exit $LASTEXITCODE
}

# Keepalive supervisor: keep the bridge (and its Edge) up across crashes / terminal closes. The
# bridge runs headless; the ONLY time a window appears is right after the bridge exits having hit a
# genuine sign-in wall -- then we relaunch the Edge VISIBLE so the user can sign in once, after
# which it returns to headless on the next loop.
Write-Host "Keepalive mode: supervising the bridge (headless). Ctrl-C to stop."
while ($true) {
    if (-not (Test-Cdp)) {
        Write-Host "CDP :$CdpPort not answering -- re-bringing up the bridge Edge (headless)..."
        Ensure-Edge -Hard | Out-Null
    }
    & $py $bridge
    if (Needs-SignIn) {
        Write-Host "Sign-in required -> opening a visible window. Sign in to M365; it continues automatically."
        Ensure-Edge -Hard -Visible | Out-Null
    }
    Start-Sleep -Seconds 3
}
