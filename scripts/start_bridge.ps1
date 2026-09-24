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
function Ensure-Edge([switch]$Hard, [switch]$Visible, [string]$Url = "") {
    # Hashtable splat (NOT array splat): array splatting an int-typed -Port mis-binds ("cannot
    # convert '-Port' to Int32"), which silently broke every supervisor launch -- the Edge only
    # ever came up when start_companion_edge.ps1 was run by hand. Hashtable splat binds by name.
    $p = @{ Port = $CdpPort; Profile = $Profile }
    if ($Visible) { $p["Foreground"] = $true } else { $p["Headless"] = $true }
    if ($Hard)    { $p["HardReset"]  = $true }
    # -Url: a VISIBLE relaunch for sign-in must open the sign-in, not the launcher's about:blank.
    if ($Url)     { $p["Url"]        = $Url }
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
#
# WAIT FOR THE WALL TO GO, NOT FOR THE FIRST MOMENT IT IS ABSENT. A headed relaunch opens the M365
# page, which is not a wall for the first seconds before it redirects to the IdP -- and the old
# loop, which only asked "is there a wall right now", demoted a window the person had not yet
# seen. So: done when the wall has been absent for three checks in a row AND either it was seen
# or a minute has passed. Returns "cleared" (the person finished), "closed" (the browser went
# away -- they closed the window) or "timeout".
#
# The keeper pause file is refreshed every check: relay/edge_recover.touch_pause's rule, because
# the minimise keeper otherwise re-minimises a window somebody is typing an MFA code into.
function Demote-ToHeadless([int]$GraceMinutes = 15) {
    if (-not (Edge-IsHeaded)) { return "not-headed" }
    $started = Get-Date
    $deadline = $started.AddMinutes($GraceMinutes)
    $sawWall = $false; $clear = 0; $outcome = "timeout"
    $pause = Join-Path $root ".fleet\edge_keep_pause"
    while ((Get-Date) -lt $deadline) {
        try { Set-Content -Path $pause -Value ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -Encoding ascii } catch { }
        if (-not (Test-Cdp)) { $outcome = "closed"; break }
        if (Needs-SignIn) { $sawWall = $true; $clear = 0 }
        else {
            $clear++
            if ($clear -ge 3 -and ($sawWall -or ((Get-Date) - $started).TotalSeconds -ge 60)) { $outcome = "cleared"; break }
        }
        Start-Sleep -Seconds 5
    }
    try { Remove-Item $pause -Force -ErrorAction SilentlyContinue } catch { }
    if ($outcome -eq "cleared") {
        Write-Host "Sign-in complete -- returning the bridge Edge to headless (no window)."
        # The need is over; the NEXT time the session expires, show the window again.
        Invoke-SignInHelper @("--rearm") | Out-Null
    } elseif ($outcome -eq "closed") {
        Write-Host "The sign-in window was closed before the sign-in finished -- back to headless; it will not be reopened until the app is started again."
    } else {
        Write-Host "Sign-in still not completed after $GraceMinutes min -- returning the Edge to headless anyway."
    }
    Ensure-Edge -Hard | Out-Null
    return $outcome
}

# ONE DEFINITION OF "ON A SIGN-IN PAGE", AND IT IS THE DOCTOR'S.
#
# This used to be its own regex -- login.microsoftonline / login.live.com / /oauth2/ -- while the
# doctor asked scripts\ensure_m365_signin.py, which also knew a federated tenant's AD FS page
# (https://<sts>/adfs/ls/). On 2026-09-24 a freshly set-up PC's bridge Edge sat on exactly that
# page: the doctor said "sign in", this said "no wall", and no window was ever shown. Asking the
# same checker the doctor asks makes that disagreement impossible rather than unlikely.
# The CsrToSSR bounce (".../chat/?redirfrom=CsrToSSR&auth=2") is still not a wall there.
$script:signinPy = Join-Path $PSScriptRoot "ensure_m365_signin.py"
function Invoke-SignInHelper([string[]]$Extra) {
    try {
        return (& $py $script:signinPy --port $CdpPort @Extra 2>&1 | Out-String)
    } catch { return "" }
}
function Needs-SignIn {
    return ((Invoke-SignInHelper @("--check-only")) -match 'VERDICT:\s*sign_in_needed')
}

# "Should the window come forward NOW?" -- asked of the checker, which also applies the rules:
# once per sign-in need (a latch; starting the app again re-arms it), and never while a chat turn
# is live on this bridge (GET /status only). Prints DECISION: <word>; only "surface" acts.
function Get-SignInDecision {
    $out = Invoke-SignInHelper @("--bridge-watch", "--status-url", "http://127.0.0.1:$BridgePort/status")
    $m = [regex]::Match($out, 'DECISION:\s*(\w+)')
    if ($m.Success) { return $m.Groups[1].Value }
    return "cannot_tell"
}

# BRING THE SIGN-IN IN FRONT OF THE PERSON, then put the browser back.
#
# The bridge process is stopped first: relaunching its Edge headed pulls the browser out from
# under it anyway, and a bridge left running would spend the sign-in reconnecting to a moving
# target. Stopped as a TREE (taskkill /T): the venv's python.exe is a launcher whose child is the
# real interpreter (measured: parent .venv\Scripts\python.exe, child Python310\python.exe). The
# launcher's job object already takes the child down with it on this install -- /T is there so
# that stays true on an interpreter whose launcher does not. Only this supervisor's own child is
# touched. The loop restarts the bridge afterwards.
function Show-SignIn($BridgeProc) {
    Write-Host "Sign-in required on the bridge Edge (:$CdpPort) -- bringing its window to the front. Sign in there; it continues automatically."
    if ($BridgeProc -and -not $BridgeProc.HasExited) {
        try { & taskkill.exe /PID $BridgeProc.Id /T /F 2>&1 | Out-Null } catch { }
        try { $BridgeProc.WaitForExit(15000) | Out-Null } catch { }
    }
    Ensure-Edge -Visible -Url "https://m365.cloud.microsoft/chat" | Out-Null
    Demote-ToHeadless | Out-Null
}

# Run the bridge ONCE, watching its browser for a sign-in wall while it runs.
#
# WHY NOT `& $py $bridge`. That blocked this supervisor for the bridge's whole life -- hours --
# so the only moment it could notice a sign-in page was after the bridge EXITED, and a bridge
# sitting on a sign-in page does not exit: it serves, and every turn fails. That is the other
# half of why the fresh PC's window never came forward. The child inherits this process's
# stdout/stderr, so bridge.log is unchanged.
$SignInPollSec = 20
function Run-BridgeWatched {
    $env:MCP_BRIDGE_SIGNIN_SUPERVISED = "1"   # the bridge reports walls; it does not surface them
    $argList = @('"' + $bridge + '"') + $bridgeArgs
    $proc = Start-Process -FilePath $py -ArgumentList $argList -NoNewWindow -PassThru
    $null = $proc.Handle                        # without this, ExitCode reads empty in PS 5.1
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds $SignInPollSec
        if ($proc.HasExited) { break }
        if ((Get-SignInDecision) -eq "surface") {
            Show-SignIn $proc
            return 0
        }
    }
    $proc.WaitForExit()
    $code = $proc.ExitCode
    # It exited on its own. If it met a wall on the way out, the same decision applies.
    if ((Get-SignInDecision) -eq "surface") { Show-SignIn $null }
    return $code
}

# Initial bring-up: headless unless the user explicitly asked to sign in.
if (-not (Ensure-Edge -Hard:$HardReset -Visible:$SignIn -Url $(if ($SignIn) { "https://m365.cloud.microsoft/chat" } else { "" }))) {
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

# A NEW SUPERVISOR IS A NEW START: the "already shown for this sign-in" mark from a previous run
# is dropped, so a person who missed the window last time sees it again after a restart/logon.
Invoke-SignInHelper @("--rearm") | Out-Null

if (-not $Keepalive) {
    # Same watched run as the supervisor: a wall is surfaced while the bridge runs, not after.
    exit (Run-BridgeWatched)
}

# Keepalive supervisor: keep the bridge (and its Edge) up across crashes / terminal closes. The
# bridge runs headless; the ONLY time a window appears is when the bridge's browser is on a
# genuine sign-in page (any IdP, including a federated AD FS) -- noticed WHILE the bridge runs
# (Run-BridgeWatched), once per sign-in need and never mid-turn. Then the Edge is relaunched
# VISIBLE and in front so the person can sign in, after which it is put back to headless.
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
    # Watched: a sign-in wall is brought to the person WHILE the bridge runs (once per need,
    # never mid-turn), then the Edge goes back to headless and this loop restarts the bridge.
    Run-BridgeWatched | Out-Null
    Start-Sleep -Seconds 3
}
