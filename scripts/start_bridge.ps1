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
#   .\start_bridge.ps1 -Fresh                   # start on a NEW conversation, do not reattach
#
# Why -Fresh exists: startup normally reattaches to the last conversation, which is usually
# what you want. It is not what you want when that conversation is the problem. Measured
# 2026-08-19, the probe had grown one to 1,340.9 MB of renderer memory -- the largest thing on
# a 16 GB machine. The bridge now recycles a conversation after MCP_BRIDGE_CONVERSATION_MAX_TURNS
# turns, but reattaching first means loading the whole thing back in before that fires, so a
# run started to escape it would spend a probe interval right back where it was.

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
    [switch]$SignIn,       # force a VISIBLE window now to sign in (normally automatic on demand)
    [switch]$CollectSettleTrace,  # record settle samples for the Stage 0 replay (see below)
    [switch]$Fresh                # do NOT reattach to the previous conversation on startup
)

$ErrorActionPreference = "Stop"
# This script lives in <repo>\scripts. $root is the REPO ROOT (.venv, bridge\ live there);
# the sibling start_companion_edge.ps1 is referenced via $PSScriptRoot below.
$root = Split-Path -Parent $PSScriptRoot

# A second -Keepalive process used to sit behind the first one's Python child and then race it
# every time that child exited. Both supervisors could hard-reset the same Edge profile, causing
# otherwise healthy overnight runs to flap. Hold a per-port, per-user-session mutex for the whole
# supervisor lifetime. The OS releases it even after a crash; an abandoned mutex is safe to adopt.
$keepaliveMutex = $null
$keepaliveMutexOwned = $false
if ($Keepalive) {
    $mutexName = "Local\M365CopilotCompanion_BridgeKeepalive_${BridgePort}_${CdpPort}"
    $keepaliveMutex = New-Object System.Threading.Mutex($false, $mutexName)
    try {
        $keepaliveMutexOwned = $keepaliveMutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $keepaliveMutexOwned = $true
    }
    if (-not $keepaliveMutexOwned) {
        Write-Host "Keepalive supervisor already owns bridge :$BridgePort / CDP :$CdpPort -- leaving it running."
        $keepaliveMutex.Dispose()
        exit 0
    }
}

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
    & (Join-Path $PSScriptRoot "start_companion_edge.ps1") @p
    return ($LASTEXITCODE -eq 0)
}

# True when the bridge Edge's CDP endpoint is actually answering on :$CdpPort.
function Test-Cdp {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$CdpPort/json/version" | Out-Null; return $true }
    catch { return $false }
}

# True when a browser process for THIS profile is running WITHOUT --headless -- i.e. it owns a
# real window that can be raised.
#
# WHY THIS EXISTS. A headed bridge Edge is not merely untidy, it is user-visible: every time the
# socket route re-keys it opens a tab to capture a token (the token is good for well under an
# hour, so this recurs all day), and creating a tab in a headed browser brings that window to the
# foreground for as long as the tab lives. From the desk it reads as a Copilot window flashing up
# over whatever the user is doing, every few dozen minutes, unprompted. One sign-in on 2026-08-26
# at 01:33 left this Edge headed for ten hours and did exactly that.
#
# --type= excludes renderer/GPU children: only the browser process carries the real command line.
function Edge-IsHeaded {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and
                           $_.CommandLine -match [regex]::Escape($Profile) -and
                           $_.CommandLine -notmatch '--type=' -and
                           $_.CommandLine -notmatch '--headless' }
        return ([bool]$procs)
    } catch { return $false }
}

# Put a headed Edge back into the background once the sign-in it was opened for is finished.
# Bounded, because "wait until the wall is gone" with no deadline is "stay headed forever" when
# nobody is at the desk -- and staying headed forever is the failure this whole helper exists to
# end. When the grace period runs out we go headless ANYWAY: an unattended sign-in wall is not a
# reason to keep flashing a window at an empty chair, and the next loop re-surfaces it if the
# bridge hits the wall again.
function Demote-ToHeadless([int]$GraceMinutes = 15) {
    if (-not (Edge-IsHeaded)) { return }
    $deadline = (Get-Date).AddMinutes($GraceMinutes)
    while ((Get-Date) -lt $deadline -and (Needs-SignIn)) { Start-Sleep -Seconds 5 }
    if (Needs-SignIn) {
        Write-Host "Sign-in still not completed after $GraceMinutes min -- returning the Edge to headless anyway."
    } else {
        Write-Host "Sign-in complete -- returning the bridge Edge to headless (no window)."
    }
    Ensure-Edge -Hard | Out-Null
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

# SETTLE TRACE COLLECTION (opt-in, off by default).
#
# The ordinary trace records only turns already past 60 seconds and keeps text_len plus the last
# 90 characters. That is the wrong population and an unusable shape for the settle-unification
# Stage 0: its primary endpoint is TRUNCATED CAPTURE, which is an early accept, so the turns that
# matter settle in seconds and wrote nothing at all -- and without a turn id, the full text and
# the turn's final text, the recorded lines cannot be grouped or labelled.
#
# Collect mode drops the age gate and records what a replay needs. It is heavier: every sample of
# every turn, with text. Hence opt-in, and hence the rotation the relay applies -- the previous
# trace stopped silently at 2 MB in the middle of a night and nobody noticed for four days.
#
# The path is ABSOLUTE. The default is relative, so the file landed in whatever directory the
# process happened to start in (scripts\.fleet\ rather than the repo's .fleet\), which is how the
# real trace came to be somewhere nobody was looking for it.
if ($CollectSettleTrace) {
    $env:MCP_SETTLE_TRACE_COLLECT = "1"
    $env:MCP_SETTLE_TRACE_PATH    = (Join-Path $root ".fleet\settle_trace.jsonl")
    Write-Host "Settle-trace collection ON -> $env:MCP_SETTLE_TRACE_PATH"
}
$py     = Join-Path $root ".venv\Scripts\python.exe"
$bridge = Join-Path $root "bridge\copilot_bridge.py"
# Splatted rather than inlined: an empty @(if ...) can reach a native command as an empty
# argument rather than as nothing at all, which the bridge would then have to ignore.
$bridgeArgs = @()
if ($Fresh) { $bridgeArgs += '--fresh' }
Write-Host ""
Write-Host "Starting bridge (headless):  UI http://127.0.0.1:$BridgePort   ->   Edge CDP :$CdpPort  (profile $Profile)"
Write-Host "(The fleet's :9222 Edge is untouched -- you can run a SWE fleet at the same time.)"

if (-not $Keepalive) {
    & $py $bridge @bridgeArgs
    # If the bridge could not reach the agent because a real sign-in wall is up, surface a window.
    if (Needs-SignIn) { Write-Host "Sign-in required -> opening a visible window once."; Ensure-Edge -Hard -Visible | Out-Null }
    exit $LASTEXITCODE
}

# Keepalive supervisor: keep the bridge (and its Edge) up across crashes / terminal closes. The
# bridge runs headless; the ONLY time a window appears is right after the bridge exits having hit a
# genuine sign-in wall -- then we relaunch the Edge VISIBLE so the user can sign in once, after
# which it is put back to headless.
#
# That last clause used to read "returns to headless on the next loop", and it was not true: the
# next pass only rebuilds the Edge when Test-Cdp FAILS, and a perfectly healthy headed Edge
# answers CDP. Nothing ever took the window away again. Worse, the loop body blocks on the bridge
# itself, so "the next loop" could be hours away -- the headed Edge from 2026-08-26 01:33 was
# still headed at 11:00, flashing a window up on every token re-key for ten hours. The demotion
# is now explicit and immediate (Demote-ToHeadless, right after the sign-in), with the top of the
# loop kept as a net for a headed Edge this supervisor did not start.
Write-Host "Keepalive mode: supervising the bridge (headless). Ctrl-C to stop."
while ($true) {
    if (-not (Test-Cdp)) {
        Write-Host "CDP :$CdpPort not answering -- re-bringing up the bridge Edge (headless)..."
        Ensure-Edge -Hard | Out-Null
    } elseif ((Edge-IsHeaded) -and -not (Needs-SignIn)) {
        Write-Host "bridge Edge has a window but no sign-in wall is up -- returning it to headless..."
        Ensure-Edge -Hard | Out-Null
    }
    & $py $bridge @bridgeArgs
    if (Needs-SignIn) {
        Write-Host "Sign-in required -> opening a visible window. Sign in to M365; it continues automatically."
        Ensure-Edge -Hard -Visible | Out-Null
        Demote-ToHeadless
    }
    Start-Sleep -Seconds 3
}
