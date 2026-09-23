# start_all.ps1 -- idempotent DAILY startup for the whole stack.
# Called by start_all.bat (double-click). Brings up, in order and ONLY IF NOT ALREADY RUNNING:
#   1. supervisor.ps1  (MCP server + devtunnel host)   -- mutex-guarded; the live tunnel is NEVER
#      killed, so re-running while a tunnel/supervisor is already up is a no-op.
#   2. the companion Edge :9222 (for the fleet / agent) -- skipped if its CDP port already answers.
#   3. start_bridge.ps1 -Keepalive (bridge :9223 + chat UI backend) -- skipped if already running.
#   4. the two WPF apps (CopilotChat, FleetCockpit)     -- launched only if not already running.
#      Use -NoUi for logon/background startup where the services should come up quietly.
# Nothing is ever stopped/killed; this only fills in what is missing. Safe to run any number of times.
param(
    [switch]$NoUi,
    # Bring up ONLY what a Copilot Studio connection test needs -- the supervisor, which is the
    # MCP server and the tunnel host. No setup gate, no browser, no bridge, no UI. quickstart
    # uses this between STEP 4 and STEP 5, because STEP 5 asks the operator to test a connection
    # and the server did not exist until STEP 7.
    [switch]$CoreOnly,
    [switch]$NoSplash
)

$ErrorActionPreference = "Continue"
# This script lives in <repo>\scripts. $root is the REPO ROOT (.env, .git, ui\ live there);
# $scriptDir is the scripts dir where the sibling launchers (supervisor.ps1,
# start_companion_edge.ps1, start_bridge.ps1) now live.
$scriptDir = $PSScriptRoot
$root = Split-Path -Parent $scriptDir
# The interpreter the helper calls below ask, and the bridge endpoint the server-swap rule
# reads. Named once so the tests can point them at a throwaway clone and a stub server
# instead of this machine's live bridge.
$script:venvPy = Join-Path $root ".venv\Scripts\python.exe"
$script:bootstrapPy = Join-Path $scriptDir "bootstrap.py"
$script:bridgeStatusUrl = "http://127.0.0.1:8765/status"

# Shared PURE helpers (Get-SupervisorArgTunnel / Get-BareTunnelName /
# Test-SupervisorTunnelDrift) for detecting a supervisor that drifted onto a
# stale/borrowed tunnel -- see tunnel_name_util.ps1's header comment. No
# top-level side effects, so dot-sourcing it here is safe.
. (Join-Path $scriptDir "tunnel_name_util.ps1")
. (Join-Path $scriptDir "update_recovery.ps1")

# The .env backfill lives in one testable place; see scripts/win/env_defaults.ps1 for why
# deciding "is this value ours or the user's" needs a record of what we wrote.
# CALLED FROM Invoke-Startup, AFTER THE SINGLE-INSTANCE LOCK, not here: it rewrites .env (not
# atomically), and the logon autostart launches this script twice at once (Startup shortcut +
# scheduled task), so two unserialised copies were two writers on one file.
. (Join-Path $PSScriptRoot "win\env_defaults.ps1")
# Where the Desktop / Startup launchers live and how the person's answer about them is recorded.
. (Join-Path $PSScriptRoot "win\convenience_marker.ps1")

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
# ---------------------------------------------------------------------------
# WHO STARTED THIS RUN (.setup\logs\start_all_runs.jsonl, one line per invocation).
#
# On 2026-09-24 full start_alls kept appearing (the splash) and could not be attributed: the
# launchers (wscript running a .vbs) had already exited, and nothing recorded who had started
# which run. So the lineage is read HERE, as the first thing the script does -- before the
# launcher is gone -- and written with the lock wait and the outcome at the end
# (New-StartAllRunRecord / Write-StartAllRunRecord). A parent that has already exited still
# leaves its pid (ParentProcessId outlives it); a live pid whose process started AFTER this one
# is a reused pid, not the parent, and is not reported as one.
# ---------------------------------------------------------------------------
function Get-LaunchLineage {
    $l = [ordered]@{ parent_pid = 0; parent_name = ""; parent_cmd = ""; grandparent_pid = 0; grandparent_name = "" }
    try {
        $me = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $PID) -ErrorAction Stop
        $l.parent_pid = [int]$me.ParentProcessId
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $me.ParentProcessId) -ErrorAction SilentlyContinue
        if ($p -and $p.CreationDate -le $me.CreationDate) {
            $l.parent_name = [string]$p.Name
            $l.parent_cmd = [string]$p.CommandLine
            $l.grandparent_pid = [int]$p.ParentProcessId
            $g = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p.ParentProcessId) -ErrorAction SilentlyContinue
            $l.grandparent_name = $(if ($g -and $g.CreationDate -le $p.CreationDate) { [string]$g.Name } else { "(exited)" })
        } else {
            $l.parent_name = "(exited)"
        }
    } catch { }
    return $l
}
$script:runStartedAt = Get-Date
$script:launch = Get-LaunchLineage
$script:lockState = "not reached"
$script:lockWaitSec = 0.0
# ---------------------------------------------------------------------------
# THE RUNNING SERVER: ONE RULE FOR "MAY IT BE STOPPED?", ASKED FROM BOTH PLACES THAT STOP IT.
#
# There were two. Invoke-PostUpdateTail (after this script's own pull) asked
# stale_server_check.py --pyside / --runlive and re-implemented decide_post_update_action by
# hand; the daily check further down (Server-Is-Outdated) asked nothing at all -- it killed
# the server whenever a TOP-LEVEL tools/relay .py was newer than the process, so a manual
# `git pull` plus a double-click dropped a live fleet or review run, and a change inside a
# subpackage (tools/auto/, relay/selfimprove/) was never noticed (new-PC analysis D12/D30).
# Both now call stale_server_check.py --server-action, which walks every file the server
# imports (tools/deploy_freshness.newer_than: tools/ and relay/ recursively, plus main.py) and
# refuses the swap while a fleet run, a review run or a bridge turn is live. Before this was
# wired the hand-written branch and decide_post_update_action were compared over all 36
# output combinations of the two old calls: identical.
# ---------------------------------------------------------------------------
function Get-ThisCheckoutServerProcesses {
    # SCOPED TO THIS CHECKOUT: another clone's main.py, or an unrelated project's, is not ours
    # to judge or to stop.
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -and ($_.CommandLine -match 'main\.py') -and ($_.CommandLine -like "*$root*") })
    } catch { return @() }
}
function Get-ServerStartEpoch {
    # Unix seconds at which the oldest of this checkout's main.py processes started, or 0 when
    # none is running or its start cannot be read (0 = nothing to judge, never "stale").
    try {
        $started = (Get-ThisCheckoutServerProcesses | Measure-Object -Property CreationDate -Minimum).Minimum
        if (-not $started) { return 0 }
        return ([DateTimeOffset]$started).ToUnixTimeMilliseconds() / 1000.0
    } catch { return 0 }
}
function ConvertTo-ServerActionResult([object[]]$Lines, [int]$ExitCode) {
    # PURE. The CLI prints "why: ..." lines and ONE verdict word last. Anything else --
    # a non-zero exit, no output, a word it does not print -- is "unknown", which every caller
    # reads as "leave the server alone": an unreadable answer must never authorise a kill.
    $text = @($Lines | ForEach-Object { [string]$_ } | Where-Object { $_ -and $_.Trim() })
    $why = @($text | Where-Object { $_ -like "why: *" } | ForEach-Object { $_.Substring(5) })
    $verdict = "unknown"
    if ($ExitCode -eq 0 -and $text.Count -gt 0) {
        $last = $text[$text.Count - 1].Trim()
        if ($last -in @("noop", "report-only", "swap-needed")) { $verdict = $last }
    }
    return @{ Verdict = $verdict; Why = $why }
}
function Get-ServerAction {
    # -ChangedPaths: the post-update form (git diff --name-only). -StartedEpoch: the daily form.
    param([string[]]$ChangedPaths = @(), [double]$StartedEpoch = 0)
    $pyExe = $script:venvPy
    $chk = Join-Path $scriptDir "stale_server_check.py"
    if (-not (Test-Path $pyExe) -or -not (Test-Path $chk)) {
        return @{ Verdict = "unknown"; Why = @("no .venv python to ask stale_server_check.py") }
    }
    $cliArgs = @($chk, "--server-action", "--fleet-dir", (Join-Path $root ".fleet"),
                 "--bridge-status", $script:bridgeStatusUrl)
    try {
        if ($StartedEpoch -gt 0) {
            $cliArgs += @("--started-epoch", $StartedEpoch.ToString("R", [Globalization.CultureInfo]::InvariantCulture))
            $out = @(& $pyExe @cliArgs 2>$null)
        } else {
            # THE PATH LIST GOES IN AS BYTES THIS FUNCTION WROTE, NOT THROUGH POWERSHELL'S PIPE.
            # `$ChangedPaths | & $pyExe ...` had PowerShell encode stdin with whatever
            # $OutputEncoding the host has: a UTF-8 one is written with a BOM, so the first path
            # reads "﻿tools/x.py", is not server code, and a changed server comes back
            # "noop" -- the verdict CI got in test_post_update_form_and_unreadable_answers
            # (reproduced here by setting $OutputEncoding = [Text.Encoding]::UTF8; an empty
            # pipe gives the same "noop"). A file of UTF-8 WITHOUT a BOM, handed over as the
            # child's stdin, and Python told to read it as UTF-8 (-X utf8), removes the host
            # setting from the question -- and a non-ASCII path from `git diff` survives too.
            $tmpIn = [System.IO.Path]::GetTempFileName()
            $tmpOut = [System.IO.Path]::GetTempFileName()
            $tmpErr = [System.IO.Path]::GetTempFileName()
            try {
                $text = ((@($ChangedPaths) | ForEach-Object { [string]$_ }) -join "`n") + "`n"
                [System.IO.File]::WriteAllText($tmpIn, $text, (New-Object System.Text.UTF8Encoding($false)))
                $argLine = @(@("-X", "utf8") + $cliArgs | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' })
                $p = Start-Process -FilePath $pyExe -ArgumentList $argLine -NoNewWindow -PassThru `
                                   -RedirectStandardInput $tmpIn -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
                $null = $p.Handle           # keeps ExitCode readable after exit (PS 5.1)
                $p.WaitForExit()
                $out = @(Get-Content -LiteralPath $tmpOut -Encoding UTF8 -ErrorAction SilentlyContinue)
                return (ConvertTo-ServerActionResult $out ([int]$p.ExitCode))
            } finally {
                Remove-Item -LiteralPath $tmpIn, $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue
            }
        }
        return (ConvertTo-ServerActionResult $out $LASTEXITCODE)
    } catch {
        return @{ Verdict = "unknown"; Why = @($_.Exception.Message) }
    }
}
function Invoke-ServerAction($Action, [string]$Tag) {
    # Acts on a Get-ServerAction result. Returns the note for the update dialog ("" if none).
    $why = ""
    if ($Action.Why -and @($Action.Why).Count -gt 0) { $why = " (" + (@($Action.Why) -join "; ") + ")" }
    switch ($Action.Verdict) {
        "swap-needed" {
            # Stop it; the supervisor (or the start path below) brings it back on the new code.
            $stopped = 0
            foreach ($p in (Get-ThisCheckoutServerProcesses)) {
                try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped++ } catch { }
            }
            if ($stopped -gt 0) {
                Write-Host "$Tag server code is newer than the running server and no run is live$why -- stopped it so it restarts on the new code"
                Start-Sleep -Seconds 2
                return "`n`nServer updated (restarting on new code)."
            }
            Write-Host "$Tag server code changed but no process of this checkout's server is running; nothing to restart"
            return "`n`nServer code updated, but the running server could not be identified; restart it manually."
        }
        "report-only" {
            Write-Host "$Tag server code is newer than the running server, but a run is live$why -- left running. The supervisor restarts it once nothing is running."
            return "`n`nServer code updated. It will take effect after the current run finishes and the server is restarted."
        }
        "unknown" {
            Write-Host "$Tag could not decide whether the running server is stale$why -- left running."
            return ""
        }
        default { return "" }
    }
}
# ---------------------------------------------------------------------------
# THE RUNNING BRIDGE: the same one-question shape as the server above, asked from the one
# place that can stop it (the daily "bridge is older than its code" check further down).
#
# Bridge-Is-Outdated (removed) called Get-ChildItem WITHOUT -Recurse over the top level of
# bridge/, tools/ and relay/ only -- a change inside a subpackage (relay/selfimprove/,
# tools/auto/) was invisible to it, exactly the D12/D30 top-level-only bug the server's own
# daily check had -- and it asked nothing about whether a chat turn was in progress, so a
# manual `git pull` plus a double-click of start_all could kill the bridge mid-turn.
# Get-BridgeAction asks stale_server_check.py --bridge-action, which scans the SAME
# directories RECURSIVELY (stale_server_check.BRIDGE_WATCHED: bridge/, relay/, tools/ -- what
# copilot_bridge.py actually imports from) and refuses the swap while the bridge's own
# /status reports a turn live (turn_running or busy) -- the identical GET-only, no-proxy
# probe --server-action already uses for its "bridge turn" input. NEVER call /stream, /goal,
# /new, /switch, /history or /upload from here: only /status, which holds no page lock.
# ---------------------------------------------------------------------------
function Get-ThisCheckoutBridgeProcesses {
    # SCOPED TO THIS CHECKOUT, exactly like Get-ThisCheckoutServerProcesses above: another
    # clone's bridge, or an unrelated project's, is not ours to judge or to stop.
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -and ($_.CommandLine -match 'copilot_bridge\.py') -and ($_.CommandLine -like "*$root*") })
    } catch { return @() }
}
function Get-BridgeStartEpoch {
    # Unix seconds at which the oldest of this checkout's bridge processes started, or 0 when
    # none is running or its start cannot be read (0 = nothing to judge, never "stale") --
    # the same rule Get-ServerStartEpoch uses.
    try {
        $started = (Get-ThisCheckoutBridgeProcesses | Measure-Object -Property CreationDate -Minimum).Minimum
        if (-not $started) { return 0 }
        return ([DateTimeOffset]$started).ToUnixTimeMilliseconds() / 1000.0
    } catch { return 0 }
}
function Get-BridgeAction {
    # Same shape as Get-ServerAction on purpose: ConvertTo-ServerActionResult parses the
    # output unchanged, and the caller reads the same three verdict words (noop /
    # report-only / swap-needed).
    $pyExe = $script:venvPy
    $chk = Join-Path $scriptDir "stale_server_check.py"
    if (-not (Test-Path $pyExe) -or -not (Test-Path $chk)) {
        return @{ Verdict = "unknown"; Why = @("no .venv python to ask stale_server_check.py") }
    }
    $epoch = Get-BridgeStartEpoch
    if ($epoch -le 0) {
        return @{ Verdict = "noop"; Why = @("no bridge process of this checkout is running") }
    }
    $cliArgs = @($chk, "--bridge-action",
                 "--started-epoch", $epoch.ToString("R", [Globalization.CultureInfo]::InvariantCulture),
                 "--bridge-status", $script:bridgeStatusUrl)
    try {
        $out = @(& $pyExe @cliArgs 2>$null)
        return (ConvertTo-ServerActionResult $out $LASTEXITCODE)
    } catch {
        return @{ Verdict = "unknown"; Why = @($_.Exception.Message) }
    }
}
function Stop-Bridge-Processes() {
    # Take the keepalive supervisor down first, otherwise it just respawns the python we are
    # about to stop and the restart silently does nothing.
    #
    # SCOPED TO THIS CHECKOUT, like the main.py stop below. Matching 'start_bridge.ps1' or
    # 'copilot_bridge.py' on the command line alone reaps another clone of this repo on the
    # same machine -- the exact over-broad kill the main.py path was already fixed to avoid.
    # Anchor both to this checkout's root so we only stop the bridge WE are restarting.
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match 'start_bridge\.ps1') -and ($_.CommandLine -like "*$root*") } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match 'copilot_bridge\.py') -and ($_.CommandLine -like "*$root*") } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
}
function Env-Value([string]$key) {
    # read a value from .env (so the Dev Tunnel name from setup_devtunnel.ps1 propagates here)
    try {
        $p = Join-Path $root ".env"
        if (Test-Path $p) {
            $m = (Get-Content $p | Where-Object { $_ -match "^\s*$([regex]::Escape($key))\s*=" } | Select-Object -First 1)
            if ($m) { return ($m -replace "^\s*$([regex]::Escape($key))\s*=\s*", "").Trim() }
        }
    } catch { }
    return ""
}

function Show-OwnedDialog([string]$body, [string]$title, [string]$buttons, [string]$icon) {
    # Show a MessageBox that is guaranteed to appear in front, even when this script
    # runs hidden (window=0 from the vbs launcher). We parent the box on a TopMost owner
    # form so it is not lost behind other windows. Returns the DialogResult.
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true
    $owner.ShowInTaskbar = $false
    $owner.StartPosition = "CenterScreen"
    $owner.Width = 1; $owner.Height = 1
    $owner.Opacity = 0
    try {
        $owner.Show()
        $owner.Activate()
        $btn = [System.Windows.Forms.MessageBoxButtons]::$buttons
        $ico = [System.Windows.Forms.MessageBoxIcon]::$icon
        return [System.Windows.Forms.MessageBox]::Show($owner, $body, $title, $btn, $ico)
    } finally {
        try { $owner.Close(); $owner.Dispose() } catch { }
    }
}

# ---------------------------------------------------------------------------
# Startup splash -- a small "M365 Companion is starting..." window shown DURING the
# few-second cold start so the wait has feedback. Rendered on THIS (main) thread with
# .Show() + DoEvents -- the SAME path as the update dialog (which is known to display), so it
# reliably appears (an earlier runspace version created the window but it never became
# visible). Best-effort: any failure leaves $splash = $null and every helper no-ops, so
# startup is NEVER blocked. It can be closed and minimised (see Start-Splash), and has a minimum
# on-screen time so a fast (already-running) startup does not just flash by unseen.
# ---------------------------------------------------------------------------
function Start-Splash {
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        Add-Type -AssemblyName System.Drawing | Out-Null
        # A WinForms Form created in a wscript-launched / -WindowStyle Hidden (SW_HIDE) powershell
        # inherits the hidden show-state and never becomes visible -- a native MessageBox does NOT
        # (that's why the update dialog shows but this form would not). Force it visible from Add_Shown.
        try {
            Add-Type -Namespace M365 -Name SplashWin -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
"@
        } catch { }
        $f = New-Object System.Windows.Forms.Form
        $f.Text = "M365 Companion"
        $f.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
        $f.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
        $f.ClientSize = New-Object System.Drawing.Size(440, 140)
        $f.TopMost = $true
        # AN ESCAPE ROUTE, ALWAYS. This was ControlBox=$false, so the splash could not be
        # closed -- and the config dialog that start_all re-opens can appear BEHIND it. The
        # combination produced an application that looked hung and could not be dismissed
        # except through the task manager. A progress window is not worth trapping someone
        # in, and if closing it early were harmful the answer would be to not need the
        # window, not to remove its close button.
        $f.ControlBox = $true
        $f.MaximizeBox = $false
        # MINIMIZABLE, because it is TopMost. A startup that waits (another start_all holding the
        # lock, a slow tunnel, a dependency update) kept this window above every other window for
        # the whole wait, and the only way out was the close button, which reads as "cancel the
        # startup". Minimising keeps the startup running and puts the window on the taskbar
        # (ShowInTaskbar), where the person can bring it back to see the status.
        $f.MinimizeBox = $true
        $f.ShowInTaskbar = $true
        $title = New-Object System.Windows.Forms.Label
        $title.Text = "M365 Companion"
        $title.Font = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)
        $title.AutoSize = $true
        $title.Location = New-Object System.Drawing.Point(22, 20)
        $f.Controls.Add($title)
        $status = New-Object System.Windows.Forms.Label
        $status.Text = "Starting M365 Companion..."
        $status.AutoSize = $false
        $status.Size = New-Object System.Drawing.Size(396, 22)
        $status.Location = New-Object System.Drawing.Point(24, 58)
        $f.Controls.Add($status)
        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
        $bar.MarqueeAnimationSpeed = 30
        $bar.Size = New-Object System.Drawing.Size(396, 18)
        $bar.Location = New-Object System.Drawing.Point(24, 92)
        $f.Controls.Add($bar)
        # Use $this (the event's form), NOT $f: Start-Splash is a function, so its local $f is gone
        # by the time Add_Shown fires from ShowDialog() in the driver -- $this is the live form.
        $f.Add_Shown({
            try { [M365.SplashWin]::ShowWindow($this.Handle, 5) | Out-Null } catch { }   # SW_SHOW
            try { [M365.SplashWin]::SetForegroundWindow($this.Handle) | Out-Null } catch { }
            try { $this.Activate(); $this.BringToFront() } catch { }
        })
        return @{ Form = $f; Status = $status; Start = (Get-Date) }
    } catch { return $null }
}
function Set-SplashStatus($splash, [string]$text) {
    try {
        if ($splash -and $splash.Status) {
            $splash.Status.Text = $text
            [System.Windows.Forms.Application]::DoEvents()
        }
    } catch { }
}
function Pump-Splash($splash) {
    try { if ($splash -and $splash.Form) { [System.Windows.Forms.Application]::DoEvents() } } catch { }
}
# (Stop-Splash removed: the splash is shown MODALLY via ShowDialog and closed by the one-shot
#  timer that drives Invoke-Startup; the minimum on-screen time is enforced inside Invoke-Startup.)

function Invoke-TunnelHealPreflight {
    # Self-heal MCP_TUNNEL_NAME/MCP_TUNNEL_URL BEFORE the supervisor starts, so it
    # hosts the tunnel this account actually owns (an .env copied from another
    # machine can otherwise name a tunnel that machine's account owns, which
    # fails to host here). Same safety envelope as Check-ForUpdates below: a
    # background job with a hard deadline so a hung/offline devtunnel CLI can
    # never delay startup, and every error is swallowed -- this step must never
    # be able to prevent the stack from coming up. Runs in BOTH normal and
    # -NoUi startup (it is non-interactive and silent either way).
    try {
        $healScript = Join-Path $scriptDir "heal_tunnel.ps1"
        if (-not (Test-Path $healScript)) { return }
        $job = Start-Job -ScriptBlock {
            param($p)
            try { & $p 2>&1 | Out-String } catch { "" }
        } -ArgumentList $healScript
        $deadline = (Get-Date).AddSeconds(25)
        while ($job.State -eq 'Running' -and (Get-Date) -lt $deadline) {
            Pump-Splash $script:splash
            Start-Sleep -Milliseconds 150
        }
        if ($job.State -eq 'Running') {
            try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
        } else {
            $out = Receive-Job $job
            if ($out -and $out.Trim()) { Write-Host ($out.Trim()) }
        }
        try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
    } catch {
        # Tunnel self-heal is best-effort only; never block startup.
    }
}

function Test-ShouldReExecAfterUpdate {
    # PURE decision helper (no I/O, no side effects) -- should start_all re-exec
    # itself after a self-update just landed new files on disk? True only when
    # ALL of: this is not already the guarded fresh re-launch, the checkout was
    # actually behind, and the pull actually succeeded. Factored out so the
    # decision can be scenario-tested in isolation without running real git/UI.
    param(
        [bool]$GuardAlreadySet,
        [int]$Behind,
        [bool]$PullSucceeded
    )
    if ($GuardAlreadySet) { return $false }
    if ($Behind -le 0) { return $false }
    if (-not $PullSucceeded) { return $false }
    return $true
}

function Get-UpdateCheckSkipReason {
    # PURE. "" when the update dialog may be shown, else the reason it is not (for the log).
    #
    # -NoUi: a background logon start has nobody to ask (unchanged).
    #
    # -CoreOnly, AND ANY cmd.exe PARENT: A PULL HERE CAN REWRITE THE BATCH FILE THAT IS RUNNING
    # US. quickstart.bat calls this script twice while it is itself executing -- -CoreOnly
    # between STEP 4 and STEP 5, and a full start at STEP 7 -- and a Yes in the dialog ran
    # `git pull` or `git reset --hard @{u}` underneath it. cmd resumes a batch file by BYTE
    # OFFSET after every line, so a quickstart.bat replaced mid-run continues at whatever now
    # sits at that offset (quickstart.bat's own STEP 3 comment describes exactly this and stops
    # after its own pull for that reason). It also asked the update question twice more after
    # the person had answered N at STEP 3 (new-PC analysis D3). -CoreOnly alone would leave
    # STEP 7 open, and quickstart.bat is not this file's to change, so the gate is the
    # mechanism itself: a cmd.exe parent means a batch file may be waiting on this process.
    # The daily launchers never have one (start_all.bat, the Desktop icon and the logon task
    # all go through wscript), so they still get the update check; a developer typing
    # `powershell -File scripts\start_all.ps1` at a cmd prompt loses it and can `git pull`.
    param([bool]$NoUi, [bool]$CoreOnly, [string]$ParentName, [string]$ParentCommandLine)
    if ($NoUi) { return "-NoUi" }
    if ($CoreOnly) { return "-CoreOnly: quickstart.bat is running and already asked about updates at STEP 3" }
    if ($ParentName -and ($ParentName -match '^(?i)cmd(\.exe)?$')) {
        $what = "a batch file"
        if ($ParentCommandLine -match '(?i)([^\\/"]+\.(bat|cmd))') { $what = $matches[1] }
        return ("started from cmd (" + $what + "): a pull now could rewrite that batch file while it runs; update with start_all.bat or git pull --ff-only")
    }
    return ""
}

function Get-ParentProcessInfo {
    # @{ Name; CommandLine } of the process that launched this one ("" when it cannot be read,
    # which Get-UpdateCheckSkipReason reads as "not a batch file").
    $info = @{ Name = ""; CommandLine = "" }
    try {
        $me = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $PID) -ErrorAction Stop
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $me.ParentProcessId) -ErrorAction Stop
        if ($parent) { $info.Name = [string]$parent.Name; $info.CommandLine = [string]$parent.CommandLine }
    } catch { }
    return $info
}

function Invoke-PostUpdateTail {
    # Shared tail run once the checkout has ACTUALLY landed the new commits --
    # by either `git pull --ff-only` (plain fast-forward) or `git reset --hard
    # @{u}` (rewritten-upstream recovery, see Check-ForUpdates). Both paths
    # need the exact same follow-up: rebuild the UI if ui/*.cs changed, tell
    # the user it's done, then re-exec so the freshly-landed code takes effect
    # for the rest of THIS startup. Kept as one function so neither path can
    # accidentally drift from the other's semantics.
    param(
        [string]$Title,
        [string]$OldRef,
        [int]$Behind
    )
    # If the update changed any ui/*.cs, rebuild the UI exes (non-fatal if it fails).
    # NOTE: the success dialog below is deliberately the SAME text regardless of which
    # strategy Check-ForUpdates used to land the update (plain fast-forward, or the
    # silent rewritten-upstream recovery) -- an end user of this app is not a git user
    # and never committed/pushed anything, so there is nothing backup-related to tell
    # them; that detail is Write-Host-logged by the caller instead, for a developer
    # reading the startup log later.
    $rebuildNote = ""
    try {
        $changed = & git -C $root diff --name-only $OldRef HEAD 2>$null
        $uiTouched = $changed | Where-Object { $_ -match '^ui/.*\.cs$' }
        if ($uiTouched) {
            $rebuildScript = Join-Path $root "ui\rebuild_ui.ps1"
            if (Test-Path $rebuildScript) {
                & $rebuildScript | Out-Null
                if ($LASTEXITCODE -eq 0) { $rebuildNote = "`n`nUI rebuilt." }
                else { $rebuildNote = "`n`nUI rebuild reported an error (will use existing exe)." }
            }
        }
    } catch { $rebuildNote = "`n`nUI rebuild skipped (error)." }

    # STALE RUNNING SERVER after a Python-side update. ui/*.cs has a rebuild path above;
    # the server's own code (relay/, tools/, main.py) had none. supervisor.ps1 is mutex-
    # guarded and treats "already running" as a no-op, and the re-exec below only restarts
    # THIS start_all -- so a server that was already up keeps executing the PRE-update code
    # it imported at startup, indefinitely. /health still answers 200, so nothing surfaces
    # it. SAFETY: we NEVER swap the server while a fleet/review run or a bridge turn is live
    # -- that would drop the run -- and if that cannot be told, it counts as live. The
    # decision is Get-ServerAction's (stale_server_check.py --server-action), the SAME one the
    # daily start asks; see the block above Get-ThisCheckoutServerProcesses for why there is
    # only one. We only STOP the stale server; the supervisor brings it back on fresh code.
    try {
        if ($changed) {
            $rebuildNote += (Invoke-ServerAction (Get-ServerAction -ChangedPaths @($changed)) "[update]")
        }
    } catch {
        Write-Host "[update] stale-server check skipped (error): $($_.Exception.Message)"
    }

    Show-OwnedDialog ("Updated to the latest version.{0}" -f $rebuildNote) $Title "OK" "Information" | Out-Null

    # DESIGN NOTE: the update above just landed new files on disk, but THIS process
    # is still running the OLD (pre-update) start_all.ps1 that was already loaded
    # into memory when it started -- without re-exec, none of the freshly-landed
    # code (this very fix included, e.g. tunnel self-heal wiring) takes effect
    # until a SECOND run. Fix: re-launch the just-updated script now, the same
    # hidden way the .vbs launcher does, preserving the original switches, and
    # let the FRESH process finish this startup (heal + stack) with the updated
    # code; THIS process then exits so only the fresh instance continues. Never
    # lets a re-exec failure stop startup: any error here is swallowed and this
    # (old) process simply falls through and keeps going on its own.
    $guardAlreadySet = ($env:MCP_STARTALL_REEXEC -eq "1")
    if (Test-ShouldReExecAfterUpdate -GuardAlreadySet $guardAlreadySet -Behind $Behind -PullSucceeded $true) {
        try {
            $selfPath = Join-Path $scriptDir "start_all.ps1"
            if (Test-Path $selfPath) {
                # EVERY SWITCH THIS RUN WAS GIVEN, and the path quoted. -CoreOnly was missing:
                # a self-restart during the core start would come back as a FULL start, walk into
                # Invoke-FirstTimeSetupGate and demand the agent URL -- which is precisely what
                # STEP 5 has not created yet, because -CoreOnly exists to run BEFORE STEP 5.
                # $selfPath was unquoted too, so an install path with a space split the argument.
                $reArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $selfPath))
                if ($NoUi) { $reArgs += "-NoUi" }
                if ($NoSplash) { $reArgs += "-NoSplash" }
                if ($CoreOnly) { $reArgs += "-CoreOnly" }
                # This run ends here without reaching the end of the script: record it now.
                $null = Write-StartAllRunRecord (Join-Path $script:diagDir "start_all_runs.jsonl") (New-StartAllRunRecord "re-exec after update")
                $env:MCP_STARTALL_REEXEC = "1"
                # HAND THE LOCK OVER FIRST. The fresh copy waits on the single-instance lock;
                # held until Environment.Exit below it would come back abandoned, which works,
                # but a release is the clean hand-over rather than the recovery path.
                Exit-StartAllLock
                Start-Process powershell -WindowStyle Hidden -ArgumentList $reArgs | Out-Null
                # Terminate THIS (old) process hard. A bare `exit` here throws a
                # System.Management.Automation.ExitException; when Invoke-Startup runs
                # inside the splash's WinForms message loop (the one-shot timer), that
                # ExitException escapes as an UNHANDLED "Microsoft .NET Framework"
                # exception dialog instead of just exiting -- and its "Continue" button
                # would leave THIS stale-code process running alongside the freshly
                # re-exec'd instance (double startup). Environment.Exit ends the process
                # cleanly from any host context (timer callback, runspace, or console).
                [System.Environment]::Exit(0)
            }
        } catch {
            # Re-exec is best-effort only -- fall through and let this (old)
            # process finish the current startup rather than leaving nothing
            # running.
        }
    }
}

# ---------------------------------------------------------------------------
# DEPENDENCY DRIFT (D5): START_ALL BRINGS THE VENV UP TO DATE ITSELF.
#
# `git pull` (or a manual pull/merge) can change requirements.txt, and nothing on the daily path
# used to install the difference: this function's predecessor only REPORTED it, telling the
# operator to run setup.bat -- and because it asked "is the stamp for this requirements.txt?"
# rather than "does the venv satisfy it?", every PC installed before the stamp existed got that
# line on every start, forever. The owner's rule since: the only remedy start_all may give for
# its own environment is running start_all.bat again, and preferably not even that.
#
# The old reasons for not installing here are each solved, not waived:
#   * hidden window, nobody reads the console -- pip's output goes to .setup\logs\deps_install_*.log
#     and a failure is COUNTED into the startup summary with pip's own last error line and that
#     path (bootstrap.py sync_deps / _last_pip_error);
#   * two starts at once (Startup .lnk + Task, 15 s apart) racing pip into one .venv -- the two
#     copies are already serialised by Enter-StartAllLock, and bootstrap.py takes an OS
#     byte-range lock (.setup\install_deps.lock) around check + install + stamp, which also
#     covers setup/quickstart running their own install_deps at the same time; a waiting
#     start_all keeps waiting while that lock is held (Test-DepsInstallInProgress);
#   * proxy / TLS interception -- Set-PipNetworkEnvironment gives pip the same environment
#     setup.bat does (ca_bundle.ps1 roots, detect_proxy.ps1 proxy), and restores it afterwards;
#   * processes running from the venv while pip rewrites it -- the install runs only when none of
#     this checkout's supervisor / server / bridge / fleet coordinator is running, or when the
#     SAME rule every server swap here uses (Get-ServerAction / stale_server_check.py
#     --server-action, requirements.txt as the changed path) says nothing is live; they are then
#     stopped first and started again by the rest of this start_all. Otherwise the install is
#     put off to the next start_all, and the summary says so in plain words;
#   * an upgrade that pip reports as successful but leaves a package missing its own files (the
#     fastmcp 2 -> 3 / fastmcp-slim case, 2026-09-24) -- bootstrap's install step checks every
#     RECORD against the disk, reinstalls what it broke, and proves the result by importing
#     main.py, for setup.bat and start_all alike.
# Runs BEFORE the supervisor is started, so a normal logon start installs first and the server
# then starts on the new packages with nothing to restart.
# ---------------------------------------------------------------------------
function Get-ThisCheckoutSupervisorProcesses {
    # All Win32_Process entries whose command line launches THIS checkout's supervisor.ps1.
    # SCOPED -- this list is what gets STOPPED. A bare name match finds anything that mentions
    # the file: measured, four matches here and three of them were shell commands that merely
    # contained the string. Stopping those would kill another checkout's supervisor, or
    # somebody's shell.
    try {
        $supPath = Join-Path $scriptDir "supervisor.ps1"
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                  Where-Object {
                      $_.CommandLine -and
                      ($_.Name -match '^(powershell|pwsh)') -and
                      ($_.CommandLine -like ("*" + $supPath + "*")) -and
                      ($_.CommandLine -notlike "*register-supervisor*")
                  })
    } catch { return @() }
}
function ConvertFrom-DepsSyncOutput([object[]]$Lines) {
    # PURE. bootstrap.py --sync-deps prints one verdict line last: "deps: ok", "deps: recorded
    # ...", "deps: installed ...", "deps: failed: <what>". Anything else -- a crash, no output --
    # is "unknown", which the caller counts as a failure (it did not bring the venv up to date).
    $v = @($Lines | ForEach-Object { [string]$_ } | Where-Object { $_ -like "deps: *" } | Select-Object -Last 1)
    if ($v.Count -eq 0) {
        $tail = @($Lines | ForEach-Object { [string]$_ } | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1)
        return @{ Verdict = "unknown"; Detail = ($(if ($tail.Count) { $tail[0].Trim() } else { "no output" })) }
    }
    $body = $v[0].Substring(6).Trim()
    foreach ($word in @("failed", "installed", "recorded", "ok")) {
        if ($body -eq $word -or $body.StartsWith($word + " ") -or $body.StartsWith($word + ":")) {
            return @{ Verdict = $word; Detail = $body.Substring($word.Length).TrimStart(":").Trim() }
        }
    }
    return @{ Verdict = "unknown"; Detail = $body }
}
function Get-DepsProblemSummary([object[]]$CheckLines) {
    # PURE. The first few "what is missing" items from --check-deps's own line, for a summary line.
    $l = @($CheckLines | ForEach-Object { [string]$_ } | Where-Object { $_ -like "The .venv does not satisfy requirements.txt:*" } | Select-Object -Last 1)
    if ($l.Count -eq 0) { return "requirements.txt is not satisfied by .venv" }
    $items = @($l[0].Substring($l[0].IndexOf(":") + 1).Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $s = ($items | Select-Object -First 3) -join "; "
    if ($items.Count -gt 3) { $s += "; and $($items.Count - 3) more" }
    return $s
}
function Test-DepsInstallInProgress {
    # Is some process holding bootstrap.py's install lock right now? Probed with the same Win32
    # byte-range lock msvcrt.locking takes (FileStream.Lock -> LockFile), released at once.
    param([string]$LockPath = (Join-Path $root ".setup\install_deps.lock"))
    if (-not (Test-Path -LiteralPath $LockPath)) { return $false }
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::Open,
                                     [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
        try { $fs.Lock(0, 1) } catch { return $true }
        try { $fs.Unlock(0, 1) } catch { }
        return $false
    } catch {
        return $false
    } finally {
        if ($fs) { $fs.Dispose() }
    }
}
function Set-PipNetworkEnvironment {
    # The environment setup.bat gives pip, for THIS process only (so for the bootstrap.py child
    # started next): the machine's trusted roots exported by ca_bundle.ps1 (TLS interception),
    # and the proxy detect_proxy.ps1 derives from Windows' own settings. Like setup.bat, a value
    # that is already set wins. Returns what was there before, for Restore-ProcessEnvironment --
    # the supervisor, server and bridge started later must not inherit a proxy meant for pip.
    $names = @("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    $saved = @{}
    foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, "Process") }
    try {
        $caScript = Join-Path $scriptDir "ca_bundle.ps1"
        if (Test-Path -LiteralPath $caScript) {
            $bundle = @(& $caScript -OutFile (Join-Path $root ".setup\ca-bundle.pem") -ExtraPem (Join-Path $root ".setup\ca-extra.pem") 2>$null |
                        ForEach-Object { [string]$_ } | Where-Object { $_ -and $_.Trim() }) | Select-Object -Last 1
            if ($bundle -and (Test-Path -LiteralPath $bundle)) {
                foreach ($n in @("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")) {
                    if (-not [Environment]::GetEnvironmentVariable($n, "Process")) {
                        [Environment]::SetEnvironmentVariable($n, [string]$bundle, "Process")
                    }
                }
            }
        }
    } catch {
        Write-Host "[deps] could not export this machine's root certificates ($($_.Exception.Message)); pip uses its own"
    }
    try {
        if (-not $env:HTTPS_PROXY) {
            $proxyScript = Join-Path $scriptDir "detect_proxy.ps1"
            $proxy = $null
            if (Test-Path -LiteralPath $proxyScript) {
                $proxy = @(& $proxyScript 2>$null | ForEach-Object { [string]$_ } | Where-Object { $_ -and $_.Trim() }) | Select-Object -Last 1
            }
            if ($proxy) {
                $env:HTTPS_PROXY = $proxy
                if (-not $env:HTTP_PROXY) { $env:HTTP_PROXY = $proxy }
                if (-not $env:NO_PROXY) { $env:NO_PROXY = "localhost,127.0.0.1,::1" }
                Write-Host "[deps] using this PC's proxy for the download: $proxy"
            }
        }
    } catch {
        Write-Host "[deps] proxy detection skipped ($($_.Exception.Message))"
    }
    return $saved
}
function Restore-ProcessEnvironment([hashtable]$Saved) {
    if (-not $Saved) { return }
    foreach ($n in @($Saved.Keys)) { [Environment]::SetEnvironmentVariable($n, $Saved[$n], "Process") }
}
function Invoke-DependencySync([string]$VenvPy, [string]$BootstrapPy) {
    # Returns the outcome word (ok / recorded / installed / deferred / failed / unknown / skipped)
    # and COUNTS every outcome that leaves the venv behind requirements.txt into
    # $script:startupFailures -- with what failed and "re-run start_all.bat", never setup.bat.
    #
    # No venv or no bootstrap.py: nothing to bring up to date; the first-time setup gate and the
    # supervisor's own "no usable Python" refusal cover "nothing is installed yet".
    if ((-not $VenvPy) -or (-not (Test-Path -LiteralPath $VenvPy)) -or
        (-not $BootstrapPy) -or (-not (Test-Path -LiteralPath $BootstrapPy))) {
        return "skipped"
    }
    # 1. The cheap question. A matching stamp answers from one hash; a missing or different stamp
    #    reads the installed metadata and, when everything is satisfied, RECORDS the stamp -- so a
    #    PC set up before the stamp existed is clean from this start on, with no install.
    try {
        $checkOut = @(& $VenvPy $BootstrapPy --check-deps 2>&1 | ForEach-Object { [string]$_ })
        $rc = $LASTEXITCODE
    } catch {
        Write-Host "[deps] dependency check could not run ($($_.Exception.Message)) -- left as-is"
        return "unknown"
    }
    if ($rc -eq 0) { return "ok" }
    if ($rc -ne 3) {
        # The venv's python itself failed; the supervisor's own Python check reports that.
        Write-Host ("[deps] dependency check exited $rc -- left as-is: " + (($checkOut | Select-Object -Last 2) -join " / "))
        return "unknown"
    }
    $need = Get-DepsProblemSummary $checkOut
    Write-Host "[deps] .venv does not satisfy requirements.txt: $need"

    # 2. NOTHING THAT RUNS FROM .venv MAY BE RUNNING WHILE PIP REWRITES IT. Measured 2026-09-24:
    #    an install under a running server and bridge left both unable to import fastmcp until the
    #    venv was repaired by hand. So: a fleet coordinator of this checkout -> not now. Anything
    #    else of ours up (the supervisor, which restarts the server on its own; the server; the
    #    bridge and its keepalive) -> ask the ONE server rule (stale_server_check.py
    #    --server-action, requirements.txt as the changed path; it reads the fleet/review run
    #    markers and the bridge's turn state). Only "swap-needed" -- nothing live -- lets this
    #    stop them; anything else, including "cannot tell", puts the install off. What is stopped
    #    here is started again by the rest of this start_all, on the new packages.
    $coords = @(Get-ThisCheckoutFleetCoordinatorPids | Where-Object { $_ })
    $sups = @(Get-ThisCheckoutSupervisorProcesses)
    $servers = @(Get-ThisCheckoutServerProcesses)
    $bridges = @(Get-ThisCheckoutBridgeProcesses)
    $why = ""
    $live = $false
    if ($coords.Count -gt 0) {
        $live = $true
        $why = "a fleet run is in progress"
    } elseif ($CoreOnly -and $bridges.Count -gt 0) {
        # -CoreOnly starts the supervisor only, so a bridge stopped here would stay down.
        $live = $true
        $why = "the chat bridge is running and this core-only start would not start it again"
    } elseif (($sups.Count + $servers.Count + $bridges.Count) -gt 0) {
        $pre = Get-ServerAction -ChangedPaths @("requirements.txt")
        if ($pre.Verdict -ne "swap-needed") {
            $live = $true
            $w = @($pre.Why | Where-Object { $_ -and ($_ -notlike "the update changed *") })
            $why = $(if ($w.Count) { $w -join "; " } else { "it could not be confirmed that nothing is running" })
        } else {
            Write-Host "[deps] nothing is in use -- stopping this checkout's supervisor, MCP server and bridge for the install; they are started again below"
            foreach ($p in $sups) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { } }
            $null = Invoke-ServerAction $pre "[deps]"
            if ($bridges.Count -gt 0) { Stop-Bridge-Processes }
            Start-Sleep -Seconds 1
        }
    }
    if ($live) {
        Write-Host "[deps] not updating now: $why"
        $script:startupFailures += "Python dependencies need updating ($need), but $why -- they will be updated automatically the next time start_all.bat runs while nothing is running"
        return "deferred"
    }

    # 3. Install, through bootstrap.py's own install_deps step (lock, re-check, pip, repair,
    #    verify by importing main.py, stamp).
    Set-SplashStatus $script:splash "Updating Python dependencies..."
    Write-Host "[deps] bringing .venv up to date with requirements.txt"
    $saved = Set-PipNetworkEnvironment
    try {
        $syncOut = @(& $VenvPy $BootstrapPy --sync-deps 2>&1 | ForEach-Object { [string]$_ })
    } catch {
        $syncOut = @("deps: failed: bootstrap.py --sync-deps could not run ($($_.Exception.Message)) -- re-run start_all.bat")
    } finally {
        Restore-ProcessEnvironment $saved
    }
    $res = ConvertFrom-DepsSyncOutput $syncOut
    switch ($res.Verdict) {
        "installed" {
            Write-Host "[deps] installed $($res.Detail)"
        }
        { $_ -eq "recorded" -or $_ -eq "ok" } {
            Write-Host "[deps] .venv satisfies requirements.txt ($($res.Verdict))"
        }
        "failed" {
            Write-Host "[deps] FAILED: $($res.Detail)" -ForegroundColor Yellow
            $script:startupFailures += "Python dependencies could not be brought up to date ($need): $($res.Detail)"
        }
        default {
            Write-Host "[deps] no verdict from bootstrap.py --sync-deps: $($res.Detail)" -ForegroundColor Yellow
            $script:startupFailures += "Python dependencies could not be brought up to date ($need): bootstrap.py --sync-deps ended without a verdict (last output: $($res.Detail)); its record is .setup\bootstrap.log -- re-run start_all.bat"
        }
    }
    return $res.Verdict
}

function Check-ForUpdates {
    # Non-fatal pre-flight: if the local checkout is behind the remote, offer to update.
    # Any failure (no git, no upstream, offline, auth needed, fetch timeout, pull fail)
    # is swallowed so daily startup is NEVER blocked. Runs once, before services start.
    #
    # LOOP GUARD: if this process is already the FRESH re-launch of a self-update (see
    # the re-exec block near the end of the try{} below), skip the update-check (and
    # therefore any further re-exec) entirely -- this makes exactly one re-exec
    # possible per real startup; it can never loop.
    if ($env:MCP_STARTALL_REEXEC -eq "1") {
        Write-Host "[update] update check skipped (already applied an update and re-launched this startup)"
        return
    }
    try {
        # 1) Must be a git work tree.
        & git -C $root rev-parse --is-inside-work-tree 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return }

        # 2) Never let git prompt for credentials (would hang the hidden process).
        $env:GIT_TERMINAL_PROMPT = '0'

        # 3) Fetch with a hard timeout and bounded retries. A Wi-Fi/VPN handover can
        #    transiently break the first connection even though the network is healthy a
        #    few seconds later. Keep every attempt bounded so startup cannot hang forever.
        $fetchExit = 1
        for ($fetchAttempt = 1; $fetchAttempt -le 3; $fetchAttempt++) {
            $job = Start-Job -ScriptBlock {
                param($r)
                $env:GIT_TERMINAL_PROMPT = '0'
                & git -C $r fetch --quiet 2>$null
                $LASTEXITCODE
            } -ArgumentList $root
            # Poll (not Wait-Job) so the splash stays painted/animated during the fetch.
            $deadline = (Get-Date).AddSeconds(15)
            while ($job.State -eq 'Running' -and (Get-Date) -lt $deadline) {
                Pump-Splash $script:splash
                Start-Sleep -Milliseconds 120
            }
            if ($job.State -eq 'Running') {
                $fetchExit = 124
                try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
            } else {
                $fetchResult = @(Receive-Job $job)
                $fetchExitRaw = ($fetchResult | Select-Object -Last 1)
                $parsedFetchExit = 1
                if ($null -ne $fetchExitRaw) {
                    [void][int]::TryParse(([string]$fetchExitRaw).Trim(), [ref]$parsedFetchExit)
                }
                $fetchExit = $parsedFetchExit
            }
            try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
            if ($fetchExit -eq 0) { break }

            Write-Host "[update] fetch attempt $fetchAttempt/3 failed (exit=$fetchExit)"
            if ($fetchAttempt -lt 3) {
                $retryUntil = (Get-Date).AddSeconds($fetchAttempt)
                while ((Get-Date) -lt $retryUntil) {
                    Pump-Splash $script:splash
                    Start-Sleep -Milliseconds 120
                }
            }
        }
        if ($fetchExit -ne 0) { return }

        # 4) How many commits behind upstream? Upstream unset -> fails -> return.
        $behindRaw = & git -C $root rev-list --count "HEAD..@{u}" 2>$null
        if ($LASTEXITCODE -ne 0) { return }
        $behind = 0
        if (-not [int]::TryParse(($behindRaw | Select-Object -First 1), [ref]$behind)) { return }
        if ($behind -le 0) { return }   # already up to date -> no dialog

        # 4b) Also work out whether we are ahead (local-only commits) and whether a plain
        #    fast-forward is possible. Together with $behind, Get-UpdateStrategy
        #    (tunnel_name_util.ps1) uses these to tell an ordinary "behind" state apart
        #    from a REWRITTEN UPSTREAM: the project's main was once force-pushed to scrub
        #    bad commit metadata, so every clone taken before that showed "behind AND
        #    ahead" (the "ahead" commits being old pre-rewrite versions of content already
        #    in the new history) and a fast-forward is impossible. This is purely an
        #    INTERNAL strategy choice -- the user is asked the exact same single question
        #    in step 5 below no matter which branch is taken; an end user of this app never
        #    commits or pushes, so nothing here is ever surfaced as a decision to them.
        $aheadRaw = & git -C $root rev-list --count "@{u}..HEAD" 2>$null
        $aheadExit = $LASTEXITCODE
        $ahead = 0
        if ($aheadExit -ne 0 -or -not [int]::TryParse(($aheadRaw | Select-Object -First 1), [ref]$ahead)) {
            $ahead = 0
        }
        & git -C $root merge-base --is-ancestor HEAD "@{u}" 2>$null | Out-Null
        $canFF = ($LASTEXITCODE -eq 0)
        $strategy = Get-UpdateStrategy -Behind $behind -Ahead $ahead -CanFastForward $canFF
        Write-Host "[update] behind=$behind ahead=$ahead canFastForward=$canFF strategy=$strategy"

        # 5) Ask the user (visible even though the host process is hidden). Phrase the count as
        #    "version(s)", NOT "commit(s)" -- commit jargon does not communicate to a general
        #    user. SAME single question regardless of $strategy: a general user has no basis to
        #    answer a different question about rewritten history, so none is ever asked.
        $title = "M365 Companion - Update available"
        $verWord = "versions"
        if ($behind -eq 1) { $verWord = "version" }
        $body  = "Your copy is {0} {1} behind the latest.`n`nUpdate to the latest now?" -f $behind, $verWord
        $answer = Show-OwnedDialog $body $title "YesNo" "Information"
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }

        if ($strategy -eq 'rewritten-upstream' -or $strategy -eq 'diverged-unknown') {
            # RECOVERY PATH: `pull --ff-only` below would just fail forever on this shape (by
            # design -- it must never silently merge/rebase over the user's own work), leaving
            # the user stuck with no way forward. Silently take a guided reset instead. Every
            # safety/diagnostic detail here is Write-Host-logged only (for a developer reading
            # the startup log later) and NEVER shown in a dialog -- the user only ever sees the
            # single question above, then either the existing generic failure dialog or the
            # existing generic success dialog, identical to the fast-forward path.
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $recovery = Invoke-RewrittenUpstreamRecovery -RepoRoot $root -Upstream "@{u}" -Timestamp $stamp
            if (-not $recovery.Success) {
                Write-Host "[update] recovery aborted: $($recovery.Error)"
                Show-OwnedDialog "Update could not complete. Your current version is kept." $title "OK" "Warning" | Out-Null
                return
            }
            Write-Host ("[update] recovery: reset to @{{u}} succeeded " +
                        "(backup=$($recovery.BackupBranch) stashed=$($recovery.StashCreated) " +
                        "stashRef=$($recovery.StashRef))")

            # Same shared tail (rebuild + generic success dialog + re-exec) as the
            # fast-forward path -- diff the ui-rebuild check from the PRE-RESET sha
            # captured above rather than HEAD@{1} (still valid after reset --hard, but
            # the explicit sha is unambiguous and documents the intent).
            Invoke-PostUpdateTail -Title $title -OldRef $recovery.OldSha -Behind $behind
            return
        }

        # strategy -eq 'fast-forward' (the only remaining possibility once $behind -gt 0,
        # since 'up-to-date' already returned above) -- EXACTLY today's existing behavior.
        # 6) Pull fast-forward only. Keep the dialog jargon-free: no raw git output (it can carry
        #    non-ASCII commit text and only confuses a general user).
        & git -C $root pull --ff-only 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Show-OwnedDialog "Update could not complete. Your current version is kept." $title "OK" "Warning" | Out-Null
            return
        }

        Invoke-PostUpdateTail -Title $title -OldRef "HEAD@{1}" -Behind $behind
    } catch {
        # Update check is best-effort only; never block startup.
        return
    }
}

# ---------------------------------------------------------------------------
# BUG 3a fix: start_all.ps1 is what the desktop icon / Startup-folder shortcut / task
# scheduler all actually launch (directly or via start_all_hidden.vbs) -- NONE of those
# paths ever run configure_env.ps1, so on a machine where it was never run by hand the
# agent URL(s) can simply never get configured. Gate on ENV STATE: if the one key with
# no built-in default (MCP_IMPL_AGENT_URL -- see agent_profiles.py, which hard-fails
# without it) is missing/blank, launch configure_env.ps1 and BLOCK until it returns,
# the same way quickstart.bat STEP 6 (line ~167) does synchronously. On an already-
# configured machine Env-Value finds a value and this is a total no-op -- it does NOT
# prompt on every startup.
# ---------------------------------------------------------------------------
function Invoke-FirstTimeSetupGate {
    $implUrl = Env-Value "MCP_IMPL_AGENT_URL"
    if ($implUrl) {
        Write-Host "[setup] MCP_IMPL_AGENT_URL is configured -- first-time setup skipped"
        return
    }
    $cfgScript = Join-Path $scriptDir "configure_env.ps1"
    if (-not (Test-Path $cfgScript)) {
        Write-Host "[setup] MCP_IMPL_AGENT_URL is not set, and scripts\configure_env.ps1 is missing -- cannot prompt for it"
        return
    }
    Write-Host "[setup] MCP_IMPL_AGENT_URL is not configured -- launching first-time setup (configure_env.ps1)"
    Set-SplashStatus $script:splash "First-time setup: enter your Copilot agent URL..."
    # Context trap this avoids: start_all can be launched HIDDEN (start_all_hidden.vbs, used by
    # the desktop icon / Startup-folder shortcut, runs `wscript ... Run(...,0)`). A WinForms
    # dialog built inside a windowless-launched powershell inherits that hidden show-state and
    # never becomes visible -- configure_env.ps1 already has an Add_Shown ShowWindow/
    # SetForegroundWindow hack to force itself onscreen for exactly this reason (see its own
    # comment), but that hack still needs a NORMAL child process to run in. So THIS ONE call is
    # intentionally NOT started hidden: Start-Process without -WindowStyle Hidden gets its own
    # fresh (normal) show-state, breaking the hidden-parent inheritance, so the setup dialog can
    # actually be seen even though start_all itself is running invisibly. -Wait blocks this
    # (interactive, first-run-only) setup step before any service starts, mirroring quickstart.bat.
    # BOUNDED, AND IT UNWINDS THE WHOLE TREE. -Wait with no timeout is what made this
    # unrecoverable: a dialog that never gets answered -- because it is behind the splash, or
    # because the person walked away -- blocked startup for ever. Killing only the parent
    # would leave the dialog orphaned on screen, so the wait cancels the process tree.
    $cfgTimeoutSec = 300
    try {
        $p = Start-Process powershell -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $cfgScript
        ) -WorkingDirectory $root -PassThru
        if (-not $p.WaitForExit($cfgTimeoutSec * 1000)) {
            Write-Host "[setup] the configuration dialog was not answered within $cfgTimeoutSec seconds -- continuing without it."
            Write-Host "[setup] run scripts\configure_env.ps1 yourself when ready, then start again."
            try {
                & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
            } catch { }
        } else {
            # READ WHAT IT SAID. The exit code used to be discarded, so cancelled, crashed and
            # saved-but-blank were indistinguishable -- and all three led straight back to the
            # same prompt on the next startup, with nothing said about which had happened.
            switch ($p.ExitCode) {
                0 { }
                2 { Write-Host "[setup] setup was cancelled -- the agent URL is still unset." }
                3 { Write-Host "[setup] the dialog was saved with the agent URL left blank -- it is still unset." }
                4 { Write-Host "[setup] the setup dialog could not run on this machine. Edit .env by hand and set MCP_IMPL_AGENT_URL." }
                default { Write-Host "[setup] configure_env.ps1 exited with code $($p.ExitCode)." }
            }
        }
    } catch {
        Write-Host "[setup] configure_env.ps1 failed to launch: $_"
    }
    $implUrl = Env-Value "MCP_IMPL_AGENT_URL"
    if (-not $implUrl) {
        Write-Host ""
        Write-Host "=========================================================================="
        Write-Host " WARNING: MCP_IMPL_AGENT_URL is still not set."
        Write-Host " Chat and Fleet will NOT work until it is configured (re-run configure_env.ps1,"
        Write-Host " or paste the URL into .env by hand). The MCP server itself will still start."
        Write-Host "=========================================================================="
        Write-Host ""
    } else {
        Write-Host "[setup] MCP_IMPL_AGENT_URL saved -- continuing startup"
    }
}

# ---------------------------------------------------------------------------
# Convenience provisioning: the Desktop icon (make_desktop_shortcut.ps1) and the logon autostart
# (register-supervisor.ps1), created from the person's recorded answer in
# .setup\convenience_provisioned (see scripts\win\convenience_marker.ps1). Runs AFTER the
# services/UIs are brought up so a failure here can never block or delay the actual startup.
#
# ONLY WHAT WAS ASKED FOR, AND ONLY WHAT IS MISSING (new-PC analysis D11). This re-ran both
# scripts on EVERY start while the record said yes -- and nothing but quickstart ever wrote the
# record, so unregister-supervisor.ps1 removed the autostart and the next start put it back.
# Now: a "yes" re-creates the thing only when it is gone, and the scripts that remove or add
# one (unregister-supervisor.ps1, make_desktop_shortcut.ps1 -Remove, and their opposites)
# record the new answer, so the file always says what the person last asked for.
# ---------------------------------------------------------------------------
# A LAUNCHER THAT EXISTS CAN STILL BE DEAD. make_desktop_shortcut.ps1 / register-supervisor.ps1
# (f27826d) point a shortcut at powershell.exe instead of wscript.exe when Windows Script Host is
# disabled -- but only when they RUN, and provisioning below ran them only for a MISSING
# shortcut. One made while WSH worked stayed on wscript.exe after a policy disabled WSH, and
# wscript then does nothing at all: no window, no error, no start. So a shortcut that targets
# wscript.exe is re-made when preflight_policy.ps1 -CheckWshOnly (the check both scripts and
# start_all.bat use) says WSH is disabled. The .lnk is read as bytes, the way
# scripts/test_shortcuts_without_wsh.py reads it -- WScript.Shell is the thing that may be gone.
function Test-ShortcutTargetsWscript([string]$LnkPath) {
    if (-not $LnkPath -or -not (Test-Path -LiteralPath $LnkPath)) { return $false }
    try {
        $b = [System.IO.File]::ReadAllBytes($LnkPath)
        # UTF-16LE at both byte parities (StringData fields land on either) plus a latin-1 pass
        # for LinkInfo's narrow LocalBasePath, where the target path is.
        $u0 = [System.Text.Encoding]::Unicode.GetString($b)
        $u1 = $(if ($b.Length -gt 1) { [System.Text.Encoding]::Unicode.GetString($b, 1, $b.Length - 1) } else { "" })
        $a = [System.Text.Encoding]::GetEncoding(28591).GetString($b)
        return (($u0 + "`n" + $u1 + "`n" + $a).ToLowerInvariant().Contains("wscript.exe"))
    } catch { return $false }
}
function Test-WshDisabled {
    # $true only when preflight_policy.ps1 -CheckWshOnly answers WSH-ENABLED=0. A check that
    # cannot run is "not known to be disabled": nothing is re-made on a guess.
    $preflight = Join-Path $scriptDir "preflight_policy.ps1"
    if (-not (Test-Path -LiteralPath $preflight)) { return $false }
    try {
        $lines = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $preflight -CheckWshOnly 2>$null |
                   ForEach-Object { ([string]$_).Trim() })
        return ($lines -contains "WSH-ENABLED=0")
    } catch { return $false }
}
function Ensure-ConvenienceProvisioning {
    try {
        $markerPath = Get-ConvenienceMarkerPath $root
        # THE MARKER RECORDS A DECISION, NOT AN ACT -- and its ABSENCE is not consent.
        # With no file there is no decision, and with no decision nothing is created.
        if (-not (Test-Path $markerPath)) {
            Write-Host "[provision] no consent on record -- creating nothing."
            Write-Host "[provision] run quickstart.bat to be asked, or scripts\make_desktop_shortcut.ps1"
            Write-Host "[provision] and scripts\register-supervisor.ps1 to do either by hand."
            return
        }
        # An older marker holds the single word "provisioned": that machine was already
        # provisioned under the previous behaviour, so re-doing it would fight the user.
        $decision = Read-ConvenienceDecision $root
        if (-not $decision -or $decision.Count -eq 0) { return }

        $wantShortcut  = ($decision['shortcut']  -eq 'yes')
        $wantAutostart = ($decision['autostart'] -eq 'yes')
        $plan = Get-ConvenienceProvisioningPlan -Decision $decision `
                    -ShortcutPresent (Test-Path (Get-DesktopLauncherPath)) `
                    -AutostartPresent (Test-Path (Get-StartupLauncherPath))

        # A present launcher that targets wscript.exe while WSH is disabled is dead: re-made like
        # a missing one (see Test-ShortcutTargetsWscript). WSH is asked only when a shortcut
        # actually targets wscript.exe, so an ordinary start pays nothing for this.
        $deadShortcut = $wantShortcut -and -not $plan.Shortcut -and (Test-ShortcutTargetsWscript (Get-DesktopLauncherPath))
        $deadAutostart = $wantAutostart -and -not $plan.Autostart -and (Test-ShortcutTargetsWscript (Get-StartupLauncherPath))
        if (($deadShortcut -or $deadAutostart) -and -not (Test-WshDisabled)) {
            $deadShortcut = $false
            $deadAutostart = $false
        }

        # a) Desktop shortcut -- only when the record says yes AND it is not there (or is dead).
        $shortcutScript = Join-Path $scriptDir "make_desktop_shortcut.ps1"
        if ($wantShortcut -and (Test-Path $shortcutScript) -and ($plan.Shortcut -or $deadShortcut)) {
            try {
                Start-Process powershell -WindowStyle Hidden -Wait -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $shortcutScript)
                ) -WorkingDirectory $root
                if ($deadShortcut) {
                    Write-Host "[provision] Desktop launcher pointed at wscript.exe, and Windows Script Host is disabled on this PC -- re-created to start through PowerShell."
                } else {
                    Write-Host "[provision] Desktop launcher was missing and your recorded answer is shortcut=yes -- re-created."
                }
                Write-Host "[provision] To remove it for good: scripts\make_desktop_shortcut.ps1 -Remove"
            } catch {
                Write-Host "[provision] desktop shortcut skipped: $_"
            }
        }

        # b) Logon autostart -- only when the record says yes AND the Startup shortcut (the
        #    primary mechanism; the scheduled task is an optional extra) is not there.
        $autostartScript = Join-Path $scriptDir "register-supervisor.ps1"
        if ($wantAutostart -and (Test-Path $autostartScript) -and ($plan.Autostart -or $deadAutostart)) {
            try {
                Start-Process powershell -WindowStyle Hidden -Wait -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $autostartScript)
                ) -WorkingDirectory $root
                if ($deadAutostart) {
                    Write-Host "[provision] logon autostart pointed at wscript.exe, and Windows Script Host is disabled on this PC -- re-registered to start through PowerShell."
                } else {
                    Write-Host "[provision] logon autostart was missing and your recorded answer is autostart=yes -- registered."
                }
                Write-Host "[provision] To turn it off for good: scripts\unregister-supervisor.ps1"
            } catch {
                Write-Host "[provision] autostart registration skipped: $_"
            }
        }
        # The file is the record of what the person chose; nothing here overwrites it.
    } catch {
        # Convenience provisioning is best-effort only; it must never affect startup.
    }
}

function Start-BackgroundSecurityUiCloser {
    # Some corporate Windows images surface a blank "Windows Security" UWP frame during
    # unattended M365/Edge startup. Closing this UI frame does not stop Defender/SecurityHealth
    # services, the tray process, Edge, bridge, tunnel, or the MCP server.
    if (-not $NoUi) { return }
    $script = @'
$ErrorActionPreference = "SilentlyContinue"
$code = @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class BgSecurityUiCloser {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
try { Add-Type $code } catch {}

function Close-WindowsSecurityFrame {
    [BgSecurityUiCloser]::EnumWindows({
        param($h, $l)
        if (-not [BgSecurityUiCloser]::IsWindowVisible($h)) { return $true }
        $title = New-Object System.Text.StringBuilder 256
        $cls = New-Object System.Text.StringBuilder 128
        [void][BgSecurityUiCloser]::GetWindowText($h, $title, $title.Capacity)
        [void][BgSecurityUiCloser]::GetClassName($h, $cls, $cls.Capacity)
        if ($title.ToString() -ne "Windows セキュリティ" -and $title.ToString() -ne "Windows Security") { return $true }
        if ($cls.ToString() -ne "ApplicationFrameWindow") { return $true }
        $rect = New-Object BgSecurityUiCloser+RECT
        [void][BgSecurityUiCloser]::GetWindowRect($h, [ref]$rect)
        $w = $rect.Right - $rect.Left
        $ht = $rect.Bottom - $rect.Top
        if ($w -lt 300 -or $ht -lt 250) { return $true }
        [void][BgSecurityUiCloser]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
        return $true
    }, [IntPtr]::Zero) | Out-Null

    Start-Sleep -Milliseconds 300
    # Last resort: close only the Windows Security UI app. Do not touch SecurityHealthService,
    # SecurityHealthSystray, ApplicationFrameHost, Edge, bridge, tunnel, or the MCP server.
    Get-Process SecHealthUI | Stop-Process -Force
}

$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Close-WindowsSecurityFrame
    Start-Sleep -Milliseconds 750
}
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    try {
        Start-Process powershell -WindowStyle Hidden -ArgumentList @(
            "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded
        ) | Out-Null
        Write-Host "[ui] Windows Security blank-frame closer armed for background startup"
    } catch {
        Write-Host "[ui] Windows Security blank-frame closer could not start: $_"
    }
}

# ---------------------------------------------------------------------------
# ONE start_all AT A TIME (new-PC analysis D14).
#
# The logon autostart launches this script TWICE: register-supervisor.ps1 installs a Startup-
# folder shortcut AND a scheduled task (15 s delay), both running start_background_hidden.vbs.
# Only the supervisor had a single-instance guard; two copies of this script ran every step
# side by side -- two .env backfills, two orphan sweeps, two fleet-resume checks that could
# both see the same interrupted run and relaunch it twice onto one state directory.
#
# GLOBAL, FOR THE SUPERVISOR'S REASON. supervisor.ps1 takes Global\m365-copilot-companion-
# supervisor so that an instance started by Task Scheduler and one started by hand cannot race,
# whatever session each runs in; this script is launched by exactly those two paths. And what
# it starts is machine-wide anyway (ports 8000/8765/9222, the supervisor's own Global mutex), so
# a second copy on the same machine has nothing of its own to start.
#
# A SECOND COPY WAITS, IT DOES NOT QUIT. Every step here is idempotent, so running after the
# first copy finishes is correct and cheap -- and quitting would lose what the second launch
# was for: a double-click during a background logon start still has to open the windows.
# ---------------------------------------------------------------------------
$script:startAllLock = $null
function Enter-StartAllLock {
    # Returns $true once this process holds the lock (or when a lock cannot be created at all:
    # Constrained Language Mode refuses New-Object on a Mutex, and a missing lock must never be
    # the reason nothing starts). $false only after waiting $TimeoutSec for another copy.
    # -KeepWaitingWhile: past $TimeoutSec, keep waiting (up to $MaxExtraSec more) while this
    # returns true. Used for "the holder is installing Python dependencies" (Invoke-
    # DependencySync), which on a slow proxied network can outlast ten minutes -- and giving up
    # then would count a failure telling the operator to close a start_all that is working.
    param([string]$Name = "Global\m365-copilot-companion-start-all",
          [int]$TimeoutSec = 600,
          [scriptblock]$OnWait = $null,
          [scriptblock]$KeepWaitingWhile = $null,
          [int]$MaxExtraSec = 3600)
    try {
        $m = New-Object System.Threading.Mutex($false, $Name)
    } catch {
        Write-Host "[lock] single-instance lock unavailable ($($_.Exception.Message)) -- continuing without it"
        return $true
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $hardDeadline = $deadline.AddSeconds($MaxExtraSec)
    $extended = $false
    $announced = $false
    while ($true) {
        $got = $false
        try {
            $got = $m.WaitOne(250)
        } catch {
            # A copy that died holding it (killed, or the Environment.Exit of a self-update)
            # leaves it ABANDONED. MEASURED on Windows PowerShell 5.1: after the holder exits
            # or is killed, WaitOne simply returns True (scripts/test_start_all_install_path.py
            # covers that). A runtime that raises AbandonedMutexException instead has still
            # handed over ownership, so that is taken the same way rather than treated as a
            # failure to start.
            $inner = $_.Exception
            while ($inner -and -not ($inner -is [System.Threading.AbandonedMutexException])) { $inner = $inner.InnerException }
            if ($inner) { $got = $true } else { throw }
        }
        if ($got) { $script:startAllLock = $m; return $true }
        if (-not $announced) {
            Write-Host "[lock] another start_all is already running on this machine -- waiting for it to finish (up to $TimeoutSec s)"
            $announced = $true
        }
        if ($OnWait) { & $OnWait }
        if ((Get-Date) -ge $deadline) {
            $keep = $false
            if ($KeepWaitingWhile -and ((Get-Date) -lt $hardDeadline)) {
                try { $keep = [bool](& $KeepWaitingWhile) } catch { $keep = $false }
            }
            if ($keep) {
                if (-not $extended) {
                    Write-Host "[lock] the other start_all is still installing Python dependencies -- waiting for it (up to $MaxExtraSec s more)"
                    $extended = $true
                }
                $deadline = (Get-Date).AddSeconds(15)
                continue
            }
            try { $m.Dispose() } catch { }
            return $false
        }
    }
}
function Exit-StartAllLock {
    if (-not $script:startAllLock) { return }
    try { $script:startAllLock.ReleaseMutex() } catch { }
    try { $script:startAllLock.Dispose() } catch { }
    $script:startAllLock = $null
}

function Get-FleetResumeSkipReason {
    # PURE. "" when start_all should run resume_interrupted_fleet.py --resume, else why not.
    #
    # THERE WERE TWO RESUMERS OF ONE MARKER. supervisor.ps1 resumes an interrupted fleet run at
    # its own startup (Invoke-FleetAutoResume), and this script ran resume_interrupted_fleet.py
    # --resume three seconds after starting that same supervisor. A resumed coordinator writes
    # its fresh marker only once Python has imported it, so for seconds both saw the old DEAD
    # pid and each could relaunch -- two coordinators on one .fleet directory, each overwriting
    # the other's status. The second concurrent start_all (see the lock above) was a third.
    #   * this run just started a supervisor that survived: that supervisor resumes at startup;
    #     this one stays out of its way.
    #   * a coordinator of this checkout is already running (either resumer's, or a live run):
    #     there is nothing to resume, whatever the marker says yet.
    #   * MCP_FLEET_AUTORESUME=0/false/no/off: the supervisor's opt-out, honoured here too.
    param([bool]$SupervisorJustStarted, [object[]]$RunningCoordinatorPids, [string]$AutoResumeSetting)
    if ($AutoResumeSetting -and ($AutoResumeSetting.Trim() -in @("0", "false", "no", "off"))) {
        return "MCP_FLEET_AUTORESUME=$AutoResumeSetting"
    }
    if ($SupervisorJustStarted) {
        return "the supervisor started by this run resumes an interrupted run itself, at its startup"
    }
    $pids = @($RunningCoordinatorPids | Where-Object { $_ })
    if ($pids.Count -gt 0) {
        return ("a fleet coordinator of this checkout is already running (pid " + ($pids -join ", ") + ")")
    }
    return ""
}
function Get-ThisCheckoutFleetCoordinatorPids {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -and ($_.CommandLine -match 'relay[\\/.]fleet_runner') -and
                                ($_.CommandLine -like "*$root*") } |
                 ForEach-Object { $_.ProcessId })
    } catch { return @() }
}

# ---------------------------------------------------------------------------
# THE TUNNEL IS PART OF "STARTED" (new-PC analysis D24).
#
# A signed-out devtunnel CLI (token lifetime, the tenant's sign-in frequency) or a tunnel that
# expired leaves the supervisor running and NOT hosting: it pauses tunnel management and writes
# one line to %TEMP%\m365-companion-supervisor.log. start_all never looked, so the daily start
# ended with zero problems while Copilot Studio could not reach this PC at all, and only
# doctor.bat's tunnel_login row said why. Checked here with the same resolution and the same
# `user show` / `show` reading as supervisor.ps1 and doctor.ps1, and COUNTED, with the command
# that fixes it.
# ---------------------------------------------------------------------------
function Resolve-DevTunnelExe {
    # Same order as supervisor.ps1 and doctor.ps1: winget link, the direct download that
    # setup_devtunnel.ps1 falls back to, then PATH. "" when there is none.
    $wingetDt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
    if (Test-Path $wingetDt) { return $wingetDt }
    $directDt = Join-Path $env:LOCALAPPDATA "devtunnel\devtunnel.exe"
    if (Test-Path $directDt) { return $directDt }
    $cmd = Get-Command devtunnel -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return ""
}
function Invoke-DevTunnelBounded([string]$Exe, [string[]]$DtArgs, [int]$TimeoutSec) {
    # Start-Job + deadline, as doctor.ps1 does: an offline CLI can hang, and this must not.
    try {
        $job = Start-Job -ScriptBlock {
            param($exe, $a)
            try { & $exe @a 2>&1 | Out-String } catch { "" }
        } -ArgumentList $Exe, $DtArgs
    } catch { return $null }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ($job.State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 150 }
    if ($job.State -eq 'Running') {
        try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
        try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
        return $null
    }
    $out = Receive-Job $job
    try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
    return [string]$out
}
function Get-TunnelLoginState([string]$UserShowOutput) {
    # PURE. supervisor.ps1's Test-DevtunnelLoggedIn reading, three-valued: an answer that is
    # neither (a timeout, an error) is "unknown", which is reported but not counted.
    if (-not $UserShowOutput) { return "unknown" }
    if ($UserShowOutput -match 'Not logged in' -or $UserShowOutput -match 'Login required') { return "signed-out" }
    if ($UserShowOutput -match 'Logged in') { return "signed-in" }
    return "unknown"
}
function Get-TunnelHostCount([string]$ShowOutput) {
    # PURE. -1: the tunnel does not exist on this account. -2: could not tell. Else N >= 0.
    if (-not $ShowOutput) { return -2 }
    if ($ShowOutput -match 'Tunnel not found') { return -1 }   # doctor.ps1's tunnel_exists reading
    if ($ShowOutput -match 'Host connections\s*:\s*(\d+)') { return [int]$Matches[1] }
    return -2
}
function Test-TunnelServing {
    # Appends to $script:startupFailures. Waits (bounded) for a host connection only when the
    # CLI is signed in and the tunnel exists, because the supervisor may have been started a
    # few seconds ago and hosting takes up to ~50 s.
    param([int]$HostWaitSec = 60, [int]$PollSec = 5)
    $dt = Resolve-DevTunnelExe
    if (-not $dt) {
        Write-Host "[tunnel] devtunnel CLI not found -- Copilot Studio cannot reach this PC." -ForegroundColor Yellow
        $script:startupFailures += "devtunnel CLI not found: install it with quickstart.bat (STEP 4) or 'winget install Microsoft.devtunnel', then start again"
        return
    }
    $login = Get-TunnelLoginState (Invoke-DevTunnelBounded $dt @('user', 'show') 10)
    if ($login -eq "signed-out") {
        Write-Host "[tunnel] devtunnel is SIGNED OUT: the tunnel is not hosted and Copilot Studio cannot reach this PC." -ForegroundColor Yellow
        Write-Host "         Run:  devtunnel user login   (opens a browser). The supervisor resumes hosting on its own within a minute." -ForegroundColor Yellow
        $script:startupFailures += "devtunnel is signed out, so the tunnel is not hosted: run 'devtunnel user login', then start again"
        return
    }
    if ($login -eq "unknown") {
        Write-Host "[tunnel] could not tell whether devtunnel is signed in (no clear answer within 10 s) -- not counted; doctor.bat checks it again." -ForegroundColor DarkGray
        return
    }
    $tn = Env-Value "MCP_TUNNEL_NAME"
    if (-not $tn) {
        Write-Host "[tunnel] MCP_TUNNEL_NAME is empty in .env: no tunnel is configured." -ForegroundColor Yellow
        $script:startupFailures += "no Dev Tunnel configured (MCP_TUNNEL_NAME is empty): run quickstart.bat (STEP 4) or powershell -File scripts\setup_devtunnel.ps1"
        return
    }
    $deadline = (Get-Date).AddSeconds($HostWaitSec)
    while ($true) {
        $n = Get-TunnelHostCount (Invoke-DevTunnelBounded $dt @('show', $tn) 10)
        if ($n -ge 1) { Write-Host "[tunnel] '$tn' is hosted ($n host connection(s))"; return }
        if ($n -eq -1) {
            Write-Host "[tunnel] Dev Tunnel '$tn' does not exist on this account (expired or deleted)." -ForegroundColor Yellow
            $script:startupFailures += ("Dev Tunnel '" + $tn + "' does not exist on this account (expired or deleted): recreate it with powershell -File scripts\setup_devtunnel.ps1 and re-paste the URL it prints into Copilot Studio")
            return
        }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds $PollSec
    }
    if ($n -eq -2) {
        Write-Host "[tunnel] could not read the state of '$tn' -- not counted; doctor.bat checks it again." -ForegroundColor DarkGray
        return
    }
    Write-Host "[tunnel] Dev Tunnel '$tn' has no host connection after $HostWaitSec s -- Copilot Studio cannot reach this PC." -ForegroundColor Yellow
    $script:startupFailures += ("Dev Tunnel '" + $tn + "' is not hosted after " + $HostWaitSec + " s: the supervisor hosts it -- see " + (Join-Path $env:TEMP 'm365-companion-supervisor.log') + ", or run doctor.bat")
}

# ---------------------------------------------------------------------------
# THE UI EXES ARE CHECKED AGAINST THEIR SOURCES, NOT BY EXISTENCE (new-PC analysis D27).
# stale_server_check.py --ui-stale reads rebuild_ui.ps1's Build lines through
# bench/ui_build_check.py -- the one parser of that list -- and names every exe that is
# missing, zero-length, or older than a source it is built from. Without .venv it cannot be
# asked, and the check falls back to the old existence test plus a zero-length test.
# ---------------------------------------------------------------------------
function ConvertFrom-UiStaleLines([object[]]$Lines) {
    # PURE. "<name> ok" / "<name> rebuild <reason>" -> @{ Names = all; Stale = @{name=reason} }
    $names = @(); $stale = @{}
    foreach ($l in @($Lines | ForEach-Object { [string]$_ })) {
        if ($l -match '^\s*([A-Za-z0-9_]+)\s+ok\s*$') { $names += $matches[1] }
        elseif ($l -match '^\s*([A-Za-z0-9_]+)\s+rebuild\s+(\S+)\s*$') { $names += $matches[1]; $stale[$matches[1]] = $matches[2] }
    }
    return @{ Names = $names; Stale = $stale }
}
function Get-UiBuildState {
    # @{ Names; Stale; Source } -- Source says whether the answer came from the sources or only
    # from the files existing.
    $pyExe = $script:venvPy
    $chk = Join-Path $scriptDir "stale_server_check.py"
    if ((Test-Path $pyExe) -and (Test-Path $chk)) {
        try {
            $out = @(& $pyExe $chk "--ui-stale" 2>$null)
            if ($LASTEXITCODE -eq 0) {
                $st = ConvertFrom-UiStaleLines $out
                if ($st.Names.Count -gt 0) { $st.Source = "sources"; return $st }
            }
        } catch { }
    }
    $st = @{ Names = @("CopilotChat", "FleetCockpit"); Stale = @{}; Source = "existence only (no .venv to read the build list)" }
    foreach ($n in $st.Names) {
        $exe = Join-Path $root ("ui\" + $n + ".exe")
        if (-not (Test-Path $exe)) { $st.Stale[$n] = "missing" }
        elseif ((Get-Item $exe).Length -eq 0) { $st.Stale[$n] = "empty" }
    }
    return $st
}
function Invoke-UiStep {
    # Build once if ANY exe needs it (rebuild_ui.ps1 always builds both), then launch whatever
    # is not running. -NoLaunch, so there is one launch path: the loop below. rebuild_ui.ps1
    # stops both apps before compiling -- the same thing the update path's rebuild does -- so a
    # window running a stale build is closed and reopened on the new one.
    Set-SplashStatus $script:splash "Opening the chat and cockpit windows..."
    $ui = Get-UiBuildState
    $counted = @{}
    if ($ui.Stale.Count -gt 0) {
        $what = (@($ui.Stale.Keys | Sort-Object | ForEach-Object { $_ + " (" + $ui.Stale[$_] + ")" }) -join ", ")
        $rebuildScript = Join-Path $root "ui\rebuild_ui.ps1"
        if (Test-Path $rebuildScript) {
            Write-Host "[4/4] UI needs building: $what [checked by $($ui.Source)] -- building both apps (~30s; open chat/cockpit windows close and reopen)..."
            Set-SplashStatus $script:splash "Building the chat and cockpit apps (~30s)..."
            $rbOut = ""
            $global:LASTEXITCODE = 0
            try {
                $rbOut = (& $rebuildScript -NoLaunch 2>&1 | Out-String)
                $rbCode = $LASTEXITCODE
            } catch {
                $rbOut = $_.Exception.Message
                $rbCode = 1
            }
            if ($rbCode -ne 0) {
                $tail = (@($rbOut -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -Last 3) -join " / "
                Write-Host "[4/4] UI rebuild FAILED: $tail -- see docs\TROUBLESHOOTING.md ('csc.exe not found' row)" -ForegroundColor Yellow
                foreach ($app in @($ui.Stale.Keys)) {
                    # COUNTED. A UI that did not build is a startup problem, and the exit
                    # code exists to carry exactly that.
                    $script:startupFailures += "${app}: rebuild failed"
                    $counted[$app] = $true
                }
            } else {
                Write-Host "[4/4] UI rebuilt"
            }
        } else {
            Write-Host "[4/4] UI needs building ($what), and ui\rebuild_ui.ps1 is missing -- see docs\TROUBLESHOOTING.md" -ForegroundColor Yellow
        }
    }
    foreach ($app in @($ui.Names)) {
        $exe = Join-Path $root ("ui\" + $app + ".exe")
        if (Get-Process $app -ErrorAction SilentlyContinue) {
            Write-Host "[4/4] ${app}: already running"
        } elseif ((Test-Path $exe) -and ((Get-Item $exe).Length -gt 0)) {
            Write-Host "[4/4] ${app}: launching"
            # THE WINDOW MAY ASK "WHO OPENED ME?": M365_LAUNCHED_BY_START_ALL=1 in ITS environment
            # only. Start-Process copies this process's environment, so it is set for the call
            # and put back at once -- nothing else start_all launches inherits it.
            $prevLaunched = [Environment]::GetEnvironmentVariable("M365_LAUNCHED_BY_START_ALL", "Process")
            [Environment]::SetEnvironmentVariable("M365_LAUNCHED_BY_START_ALL", "1", "Process")
            try {
                Start-Process $exe
            } finally {
                [Environment]::SetEnvironmentVariable("M365_LAUNCHED_BY_START_ALL", $prevLaunched, "Process")
            }
        } else {
            Write-Host "[4/4] ${app}: no usable ui\$app.exe -- the chat/cockpit window cannot open" -ForegroundColor Yellow
            if (-not $counted[$app]) {
                $script:startupFailures += ("${app}: ui\" + $app + ".exe is missing or empty and was not rebuilt -- run powershell -File ui\rebuild_ui.ps1")
            }
        }
    }
}

# ---------------------------------------------------------------------------
# A BACKGROUND START THAT FAILED HAS TO LEAVE SOMETHING SOMEBODY SEES.
#
# The daily launchers run this hidden (start_all_hidden.vbs, start_background_hidden.vbs: window
# 0, exit code unread), so the failure list printed at the end went to a window nobody has. Two
# things now carry it out: .setup\logs\start_all_summary.txt, rewritten on EVERY run (so a clean
# start clears yesterday's list), next to doctor_summary.txt; and, when there are failures and
# no visible console, a desktop notification through tools/notify_ops.notify_desktop -- the
# helper the supervisor and heal_tunnel.ps1 already use -- listing them and pointing at
# doctor.bat. A visible console (quickstart, a manual run) already shows the list, so it is not
# repeated there.
# ---------------------------------------------------------------------------
function Write-StartupSummary([string]$Path, [string[]]$Failures, [string]$Mode) {
    try {
        $lines = @(("failures=" + @($Failures).Count),
                   ("when=" + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")),
                   ("mode=" + $Mode))
        foreach ($f in @($Failures)) { $lines += ("- " + (Hide-Secrets ([string]$f))) }
        if (@($Failures).Count -gt 0) { $lines += "fix: run doctor.bat for the specific fix for each line" }
        $dir = Split-Path -Parent $Path
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $tmp = $Path + ".tmp"
        [System.IO.File]::WriteAllLines($tmp, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $Path -Force
        return $true
    } catch { return $false }
}
function Get-StartAllMode {
    if ($CoreOnly) { return "core (-CoreOnly)" }
    if ($NoUi) { return "background (-NoUi)" }
    return "full"
}
function New-StartAllRunRecord([string]$Outcome) {
    # One line of start_all_runs.jsonl -- see Get-LaunchLineage's header.
    $sw = @()
    if ($NoUi) { $sw += "-NoUi" }
    if ($NoSplash) { $sw += "-NoSplash" }
    if ($CoreOnly) { $sw += "-CoreOnly" }
    $l = $script:launch
    if (-not $l) { $l = @{} }
    return [ordered]@{
        ts               = $script:runStartedAt.ToString("yyyy-MM-ddTHH:mm:ss.fffzzz")
        end              = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.fffzzz")
        pid              = $PID
        mode             = (Get-StartAllMode)
        switches         = $sw
        reexec           = ($env:MCP_STARTALL_REEXEC -eq "1")
        parent_pid       = $l.parent_pid
        parent_name      = $l.parent_name
        parent_cmd       = (Hide-Secrets ([string]$l.parent_cmd))
        grandparent_pid  = $l.grandparent_pid
        grandparent_name = $l.grandparent_name
        lock             = $script:lockState
        lock_wait_s      = $script:lockWaitSec
        failures         = @($script:startupFailures).Count
        outcome          = $Outcome
    }
}
function Write-StartAllRunRecord([string]$Path, $Record, [int]$Keep = 500) {
    # Append one JSON line, keeping only the last $Keep. Written while the start_all lock is still
    # held, so two copies do not interleave their rewrites. Never throws.
    try {
        $line = ($Record | ConvertTo-Json -Compress -Depth 3)
        $dir = Split-Path -Parent $Path
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $lines = @()
        if (Test-Path -LiteralPath $Path) {
            $lines = @([System.IO.File]::ReadAllLines($Path) | Where-Object { $_ -and $_.Trim() })
        }
        $lines += $line
        if ($lines.Count -gt $Keep) { $lines = $lines[($lines.Count - $Keep)..($lines.Count - 1)] }
        $tmp = $Path + ".tmp"
        [System.IO.File]::WriteAllLines($tmp, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $Path -Force
        return $true
    } catch { return $false }
}
function Test-ShouldNotifyStartupFailures([int]$Count, [bool]$NoUi, [bool]$ConsoleVisible) {
    # PURE.
    if ($Count -le 0) { return $false }
    return ($NoUi -or -not $ConsoleVisible)
}
function Test-ConsoleVisible {
    # Is there a console window a person can see? $false for the hidden VBS launches (window 0)
    # and when this cannot be told -- the notification is the safer error.
    try {
        if (-not ("M365.ConsoleVis" -as [type])) {
            Add-Type -Namespace M365 -Name ConsoleVis -MemberDefinition @"
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
"@ -ErrorAction Stop
        }
        $h = [M365.ConsoleVis]::GetConsoleWindow()
        return (($h -ne [IntPtr]::Zero) -and [M365.ConsoleVis]::IsWindowVisible($h))
    } catch { return $false }
}
function Send-StartupFailureNotice([string]$SummaryPath) {
    # Best effort. The script reads the "- " lines itself from the summary file and gets every
    # value through argv, so no failure text is ever spliced into code.
    try {
        $py = $script:venvPy
        if (-not (Test-Path $py)) { return $false }
        $code = "import sys; sys.path.insert(0, sys.argv[1]); from tools.notify_ops import notify_desktop; " +
                "ls = [l[2:].strip() for l in open(sys.argv[2], encoding='utf-8') if l.startswith('- ')]; " +
                "notify_desktop('M365 Companion: %d startup problem(s)' % len(ls), chr(10).join(ls[:4] + ['Run doctor.bat for the fix for each.']), launch=sys.argv[3])"
        $uri = ([Uri]$SummaryPath).AbsoluteUri
        & $py -c $code $root $SummaryPath $uri 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

# Everything that brings the stack up, as ONE function so it can run either INSIDE the splash's
# message loop (a one-shot timer, so the modal splash stays visible while this runs) OR directly
# as a fallback if the splash cannot be shown. Status updates target $script:splash (no-op if null).
function Invoke-Startup {
    # FIRST, BEFORE ANYTHING IS WRITTEN OR STARTED: one copy of this script at a time (see
    # Enter-StartAllLock). The splash keeps painting while a second copy waits.
    $lockT0 = Get-Date
    $gotLock = Enter-StartAllLock -OnWait {
        Set-SplashStatus $script:splash "Another startup is already running -- waiting for it to finish..."
    } -KeepWaitingWhile { Test-DepsInstallInProgress }
    $script:lockWaitSec = [math]::Round(((Get-Date) - $lockT0).TotalSeconds, 1)
    $script:lockState = $(if (-not $gotLock) { "timed out" } elseif ($script:lockWaitSec -ge 1) { "got after waiting" } else { "got" })
    if (-not $gotLock) {
        $script:lockTimedOut = $true
        $script:startupFailures += "another start_all.ps1 held the startup lock for 10 minutes; this one did not start anything alongside it -- close it (Task Manager) and start again"
        return
    }
    Ensure-EnvDefaults

    Start-BackgroundSecurityUiCloser

    # First-time setup gate (BUG 3a): must run before anything is started, and before the
    # update-check/splash sequence below so its status text is not overwritten mid-prompt.
    # Background logon startup must never show interactive setup; a manual launch still does.
    if ($NoUi) {
        Write-Host "[setup] first-time interactive setup skipped (-NoUi)"
    } elseif ($CoreOnly) {
        # THE GATE ASKS FOR THE THING STEP 5 IS ABOUT TO CREATE. It demands MCP_IMPL_AGENT_URL,
        # which does not exist until the operator has made the agent -- which is what they are
        # opening Copilot Studio to do. Asking here would be a deadlock.
        Write-Host "[setup] first-time interactive setup skipped (-CoreOnly)"
    } else {
        Invoke-FirstTimeSetupGate
    }

    # A .ENV CARRIED FROM ANOTHER PC IS PRESENT AND UNUSABLE, WHICH NOTHING WAS LOOKING FOR.
    #
    # MCP_UNLOCK_PASSWORD_PROTECTED is a DPAPI value bound to one Windows account on one machine.
    # Copy .env to a new PC and it decrypts nowhere: unlock() then reports the OTHER variable as
    # "not configured", every mutating and executing tool is refused, and the stack looks healthy
    # the whole time because startup does not need the password. Three days went into "the server
    # will not start" on a server that was starting perfectly.
    #
    # setup.ps1 cannot fix it -- it writes .env only when there is none, and a copied file exists.
    # So the repair belongs here, on the path that runs every time. It is NOT interactive: the
    # password is generated (setup.ps1 makes it with RNGCryptoServiceProvider), so a machine that
    # cannot read the stored one can simply establish its own. Runs under -NoUi too, because a
    # background logon start is exactly when nobody is present to be asked.
    try {
        $venvPy = Join-Path $root ".venv\Scripts\python.exe"
        if (Test-Path $venvPy) {
            $envFile = Join-Path $root ".env"
            # A SCRIPT FILE, NOT A `-c` PAYLOAD -- the form that made the unlock CHECK
            # unable to fail. It prints one line: noop:<why> / repaired:<password> / failed:<why>.
            $repairScript = Join-Path $scriptDir "repair_unlock.py"
            $repair = ""
            if (Test-Path $repairScript) {
                # BY PREFIX, NOT BY POSITION. 2>&1 merges stderr into the stream, so the
                # LAST line is whatever came last -- one deprecation warning and the verdict
                # matches neither "repaired:*" nor "failed:*", losing both the new password and
                # the failure recording. repair_unlock.py emits exactly one verdict line.
                $repairLines = @(& $venvPy $repairScript $envFile 2>&1)
                $repair = ($repairLines | Where-Object {
                    $_ -match '^(noop|repaired|failed|error):'
                } | Select-Object -Last 1)
            }
            if ($repair -like "repaired:*") {
                # THE OPERATOR HAS TO BE TOLD, BUT NOT THE VALUE. The repair generates a NEW
                # password and writes it to .env itself; repair_unlock.py deliberately no longer
                # returns the cleartext, because this stdout is captured here and can reach logs,
                # and the repo is public. So we announce that it changed and point at how to read
                # it, rather than echoing a secret. $repair now carries only a non-secret note.
                Write-Host ""
                Write-Host "  ============================================================"
                Write-Host "  The unlock password could not be decrypted by this Windows"
                Write-Host "  account, so a NEW one was established and written to .env."
                Write-Host ""
                Write-Host "  Any password you brought from another PC no longer works here."
                Write-Host "  Read the new value with: scripts\copilot_studio_values.ps1"
                Write-Host "  (The fleet and bridge unlock themselves.)"
                Write-Host "  ============================================================"
                Write-Host ""
            } elseif ($repair -like "failed:*") {
                Write-Host ("[setup] UNLOCK PASSWORD REPAIR FAILED: " + $repair.Substring("failed:".Length))
                Write-Host "[setup] mutating tools (write_file, run_python, shell) will be refused until this is fixed."
                # COUNTED, not only printed. The exit code is the number of startup problems and
                # nothing was feeding it: this one means every mutating tool is refused.
                $script:startupFailures += ("unlock password repair failed: " + $repair.Substring("failed:".Length))
            }
        }
    } catch {
        # Never fatal. A machine that cannot run this still starts; it just keeps the fault it had.
        Write-Host "[setup] unlock-password check skipped ($($_.Exception.Message))"
    }

    # Pre-flight update check (best-effort, non-blocking). Runs once before any service starts.
    # Skipped when nobody should be asked, or when a pull could rewrite a batch file that is
    # running this script -- see Get-UpdateCheckSkipReason for the exact gate and why.
    $parentInfo = Get-ParentProcessInfo
    $updateSkip = Get-UpdateCheckSkipReason -NoUi ([bool]$NoUi) -CoreOnly ([bool]$CoreOnly) `
                                            -ParentName $parentInfo.Name -ParentCommandLine $parentInfo.CommandLine
    if ($updateSkip) {
        Write-Host "[update] update check skipped ($updateSkip)"
    } else {
        Set-SplashStatus $script:splash "Checking for updates..."
        Check-ForUpdates
    }

    # Dependency drift (D5): the venv is brought up to date with requirements.txt HERE -- after
    # the update check, so a requirements.txt that pull just changed is installed on this start,
    # and before the supervisor (and so the server) is started below. See the block above
    # ConvertFrom-DepsSyncOutput. Whatever it cannot fix is counted into
    # $script:startupFailures with what failed and "re-run start_all.bat"; never setup.bat.
    try {
        $null = Invoke-DependencySync $script:venvPy $script:bootstrapPy
    } catch {
        Write-Host "[deps] dependency update skipped ($($_.Exception.Message))"
        $script:startupFailures += "Python dependencies could not be checked: $($_.Exception.Message) -- re-run start_all.bat"
    }

    # Dev Tunnel self-heal (best-effort, non-blocking, runs even under -NoUi):
    # repoints MCP_TUNNEL_NAME/MCP_TUNNEL_URL to a tunnel this account actually
    # owns, BEFORE the supervisor (below) hosts it.
    Set-SplashStatus $script:splash "Checking the Dev Tunnel..."
    Invoke-TunnelHealPreflight

    Write-Host "=== Daily startup (idempotent -- already-running parts are left as-is) ==="

    # 1) Supervisor = MCP server + Dev Tunnel host. Its own global mutex makes a second instance
    #    exit quietly, and it never touches a live `devtunnel host` -- so a live tunnel is kept.
    #    EXCEPT: a running supervisor that has drifted onto a different tunnel than .env
    #    currently names (e.g. heal_tunnel.ps1 repointed .env to this account's own tunnel
    #    while the supervisor was already hosting a borrowed one from a copied .env) is NOT
    #    "left as-is" -- it is actively polluting someone else's tunnel while this machine's
    #    own tunnel stays unhosted, and doctor.ps1's tunnel_serving check would stay red
    #    forever. Detect that with Test-SupervisorTunnelDrift and restart on the correct
    #    tunnel; otherwise behave exactly as before.
    Set-SplashStatus $script:splash "Starting the MCP server and Dev Tunnel..."
    function Start-FreshSupervisor([string]$tn) {
        # QUOTED. -ArgumentList elements are joined with spaces and not quoted, so an
        # install path containing one becomes two arguments and the launch fails.
        $supArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File",
                     ('"{0}"' -f (Join-Path $scriptDir "supervisor.ps1")))
        if ($tn) { $supArgs += @("-TunnelName", ('"{0}"' -f $tn)); Write-Host "[1/4] supervisor (MCP server + tunnel '$tn'): starting" }
        else     { Write-Host "[1/4] supervisor (MCP server + tunnel): starting" }
        # ITS STARTUP ERROR WAS GOING NOWHERE -- the same hole d15a834 closed for the server,
        # still open on the thing that launches it. Hidden window, no redirection: when the
        # supervisor dies on startup (Constrained Language Mode refusing New-Object Mutex,
        # AppLocker blocking the script, a dot-source that fails, or its own new "REFUSING TO
        # RUN: no usable Python" exit) the reason is discarded, and doctor then says "double-
        # click start_all.bat" -- the operation that just ran.
        $supErr = Join-Path $script:diagDir "supervisor.err.log"
        try {
            # DID IT SURVIVE? This was fire-and-forget: supervisor.ps1 exits 3 when there is
            # no usable Python, and start_all printed "starting" and moved on regardless --
            # asymmetric with the unlock repair, the bridge port and the UI rebuild, which are
            # all counted. A supervisor that died is the difference between a stack that comes
            # up in ninety seconds and one that never comes up at all.
            $supProc = Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs -RedirectStandardError $supErr -PassThru
            if ($supProc) {
                # It refuses within its first few statements, long before the health loop, so a
                # short wait separates "died on startup" from "running".
                $null = $supProc.WaitForExit(3000)
                if ($supProc.HasExited) {
                    $why = "supervisor exited immediately (code $($supProc.ExitCode))"
                    try {
                        if (Test-Path $supErr) {
                            $supLines = @(Get-Content $supErr -Tail 5 -ErrorAction Stop |
                                          Where-Object { $_.Trim() })
                            if ($supLines.Count -gt 0) { $why = $why + ": " + ($supLines -join " / ") }
                        }
                    } catch { }
                    Write-Host ("[1/4] " + $why) -ForegroundColor Yellow
                    $script:startupFailures += $why
                    $script:supervisorDied = $true
                } else {
                    # It survived its first statements, so it will reach its own startup-time
                    # fleet resume; the resume step below leaves that to it.
                    $script:supervisorStartedHere = $true
                }
            }
        } catch {
            # Starting it matters more than capturing it: a previous instance holding the file
            # must not be able to keep the stack down.
            Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs
        }
    }
    function Get-RunningSupervisorProcesses {
        # Normally zero or one; an array so a drift-restart can stop every match. One scoped
        # definition, shared with Invoke-DependencySync (see Get-ThisCheckoutSupervisorProcesses).
        return @(Get-ThisCheckoutSupervisorProcesses)
    }
    # main.py が自分のソースより古ければ落とす。supervisor が居れば数十秒で拾い直し、
    # 居なければ下の起動経路が立ち上げる。トンネルには触らない。
    # ここが無かった頃、main.py を直しても古いプロセスが残り、直したはずの説明文が
    # 配られ続けた（直っていないのか反映されていないのかが切り分けられない）。
    # NOT WHILE A RUN IS LIVE, AND NOT ONLY THE TOP LEVEL: the same decision the post-update
    # tail uses (Get-ServerAction). A server with nothing newer is a no-op; a live fleet/review
    # run or bridge turn leaves it running and says so.
    $srvStarted = Get-ServerStartEpoch
    if ($srvStarted -gt 0) {
        Invoke-ServerAction (Get-ServerAction -StartedEpoch $srvStarted) "[1/4] MCP server:" | Out-Null
    }
    $envTn = Env-Value "MCP_TUNNEL_NAME"
    $runningSupervisors = Get-RunningSupervisorProcesses
    if ($runningSupervisors.Count -eq 0) {
        Start-FreshSupervisor $envTn
    } else {
        $runCmdLine = $runningSupervisors[0].CommandLine
        if (Test-SupervisorTunnelDrift -RunningCommandLine $runCmdLine -EnvTunnelName $envTn) {
            $runTn = Get-SupervisorArgTunnel $runCmdLine
            Write-Host "[1/4] supervisor is hosting a STALE tunnel ('$runTn') != .env ('$envTn') -- stopping and restarting on the correct tunnel"
            try {
                foreach ($p in $runningSupervisors) {
                    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                }
                # Also stop the stale devtunnel host process for the OLD (borrowed) name --
                # same targeted match supervisor.ps1 itself uses -- so this machine stops
                # polluting the borrowed tunnel. NEVER a bare `Get-Process devtunnel |
                # Stop-Process`: that would reap an interactive `devtunnel login` or any
                # unrelated tunnel the user hosts by hand.
                if ($runTn) {
                    Get-CimInstance Win32_Process -Filter "Name='devtunnel.exe'" -ErrorAction SilentlyContinue |
                        Where-Object { $_.CommandLine -match '\bhost\b' -and $_.CommandLine -match [regex]::Escape($runTn) } |
                        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                }
            } catch {
                # Best-effort: a failure to stop the stale supervisor/host must never block startup.
            }
            Start-FreshSupervisor $envTn
        } else {
            Write-Host "[1/4] supervisor (MCP server + tunnel): already running -- left as-is"
        }
    }

    # 1b) Collect browsers left behind by a previous session, BEFORE starting anything.
    #
    # The measurement series launches its own Edge on :9224 and, until 2026-08-26, had no
    # teardown of any kind -- so an interrupted or finished series left a browser running
    # with a Copilot tab and nobody responsible for it. One was found idling at 331 MB hours
    # afterwards. The series now tears down after itself, but a teardown only runs when the
    # script survives to run it; a Ctrl-C is covered, a hard kill or a lost power cable is
    # not. This sweep is the part that does not depend on anyone exiting cleanly.
    #
    # Safe to run first: the reaper stops a managed browser only when the process that owns
    # it is not running, so a fleet run or bridge that is already up is left strictly alone.
    # Report-and-stop rather than silence, because reclaiming a browser somebody is watching
    # in Task Manager should be explainable afterwards.
    $reaper = Join-Path $root "scripts\win\reap_orphan_edge.py"
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if ((Test-Path $reaper) -and (Test-Path $py)) {
        try {
            & $py $reaper --stop 2>&1 | ForEach-Object { Write-Host "      $_" }
        } catch { Write-Host "      (orphan sweep skipped: $($_.Exception.Message))" }
    }

    # 1b-2) A fleet run whose coordinator died mid-flight gets continued, BEFORE the reaper
    # below runs. Order matters and is not incidental: the reaper clears the active-run
    # marker, which is the same file this reads to know there is anything to resume. A
    # resumed run writes a fresh marker with a live pid, so the reaper then leaves it alone.
    #
    # Everything this needs already existed -- the marker carries a precomputed resume argv,
    # should_auto_resume() states the rule -- and nothing called it, so an interrupted run
    # simply stayed interrupted and the way anyone found out was that the answer never came.
    #
    # ONCE, NOT TWICE: see Get-FleetResumeSkipReason for the two other resumers this used to
    # race (the supervisor it had just started, and a second copy of this script).
    if (Test-Path $py) {
        $resumer = Join-Path $root "scripts\win\resume_interrupted_fleet.py"
        if (Test-Path $resumer) {
            $resumeSkip = Get-FleetResumeSkipReason -SupervisorJustStarted ([bool]$script:supervisorStartedHere) `
                              -RunningCoordinatorPids (Get-ThisCheckoutFleetCoordinatorPids) `
                              -AutoResumeSetting ([string]$env:MCP_FLEET_AUTORESUME)
            if ($resumeSkip) {
                Write-Host "      fleet resume check skipped: $resumeSkip"
                # AND THE REAP BELOW WITH IT, unless auto-resume is switched off. The reaper
                # deletes a dead run's marker -- the very file a resume reads -- and the
                # supervisor that is about to resume (or a coordinator that has not written
                # its fresh marker yet) would find it gone. The supervisor reaps phantoms on
                # its own loop (Invoke-FleetReap), after its resume, in the right order.
                if ($resumeSkip -notlike "MCP_FLEET_AUTORESUME=*") { $script:fleetLeftToSupervisor = $true }
            } else {
                try {
                    & $py $resumer --resume 2>&1 | ForEach-Object { Write-Host "      $_" }
                } catch { Write-Host "      (resume check skipped: $($_.Exception.Message))" }
            }
        }
    }

    # 1c) And the sidecar files a fleet coordinator that died mid-run left behind. Until now
    # relay/fleet_reaper.py had no entry point and nothing in the repository referenced it,
    # so a phantom run kept claiming to be live: the cockpit showed workers "running" for
    # ever, and Stop/Pause wrote to a commands file nothing was left alive to read.
    #
    # It refuses to touch a run whose pid is alive, never relaunches anything and never
    # raises, so it is safe here -- including when the user clicks start_all while a real
    # run is going.
    if ($script:fleetLeftToSupervisor) {
        Write-Host "      phantom-run sweep left to the supervisor (it resumes first, then reaps)"
    } elseif (Test-Path $py) {
        try {
            & $py -m relay.fleet_reaper --reap 2>&1 | ForEach-Object { Write-Host "      $_" }
        } catch { Write-Host "      (phantom-run sweep skipped: $($_.Exception.Message))" }
    }

    if ($CoreOnly) {
        # Everything a connection test needs is up. The browser, the bridge and the UI are not
        # part of that path and are left for the full run at STEP 7.
        Write-Host "[core] server and tunnel are up; browser, bridge and UI left for STEP 7"
        return
    }

    # 2) Companion Edge :9222 (the fleet / agent Edge). Idempotent; skip if the port answers.
    Set-SplashStatus $script:splash "Starting the agent browser..."
    if (Port-Up 9222) {
        Write-Host "[2/4] companion Edge :9222: already up"
    } else {
        Write-Host "[2/4] companion Edge :9222: starting (headless)"
        try { & "$scriptDir\start_companion_edge.ps1" -Headless | Out-Null } catch { Write-Host "      (companion Edge launch returned: $_)" }
    }

    # 3) Bridge :9223 + chat backend (start_bridge -Keepalive). Skip if already up.
    #
    # ...unless the code on disk is newer than the process running it. Leaving a running
    # bridge alone is what keeps this script safe to double-click mid-session, but it also
    # means pulling a fix changes nothing until someone happens to restart: a bridge that had
    # been up since the previous day was still serving the previous day's code, so every fix
    # shipped that day was inert and the bug they fixed looked unfixed. Restarting only when
    # the source is actually newer keeps the idempotent behaviour for the ordinary case and
    # makes "pull, then click this" enough on its own.
    #
    # BUT NOT MID-TURN. Get-BridgeAction (see its block above) withholds the restart while
    # the bridge's own /status reports a turn live, exactly like the server-swap rule
    # withholds a kill during a live fleet/review run -- a manual `git pull` plus a
    # double-click must not be able to drop what the operator is doing right now.
    Set-SplashStatus $script:splash "Starting the chat bridge..."
    $bridgeAction = Get-BridgeAction
    switch ($bridgeAction.Verdict) {
        "swap-needed" {
            $why = ""
            if ($bridgeAction.Why -and @($bridgeAction.Why).Count -gt 0) { $why = " (" + (@($bridgeAction.Why) -join "; ") + ")" }
            Write-Host "[3/4] bridge: code is newer than the running process and no turn is live$why -- restarting"
            Stop-Bridge-Processes
            Start-Sleep -Seconds 2
        }
        "report-only" {
            Write-Host "[3/4] bridge is running old code and will be refreshed on the next start when idle"
        }
        default { }
    }
    # A KEEPALIVE PROCESS IS NOT A SERVING BRIDGE. This branch asked only whether the wrapper
    # existed, so a wedged python holding :8765 behind a live keepalive reported "already
    # running" and nothing looked further -- which is how it stayed that way for five and a half
    # hours. Both, or fall through to the diagnosis below.
    if ((Proc-Running 'start_bridge\.ps1') -and (Http-Up "http://127.0.0.1:8765/conv")) {
        Write-Host "[3/4] bridge keepalive: already running and serving"
    } elseif (Http-Up "http://127.0.0.1:8765/conv") {
        Write-Host "[3/4] bridge :8765: already serving (no keepalive supervisor, but up)"
    } else {
        # STARTING ANOTHER CANNOT HELP IF THE PORT IS ALREADY TAKEN. /conv is the liveness
        # probe, and a bridge started outside the venv answers / but not /conv -- so this branch
        # concluded "down", launched another that could not bind, and repeated. Seen running for
        # five and a half hours with four bridge processes alive, each pass reporting "starting".
        #
        # NAMED, NOT KILLED: the owner is a process this stack did not start, and the rule here
        # is not to touch those. It is recorded as a startup failure instead, which the exit code
        # now carries.
        $portOwner8765 = $null
        try {
            $portOwner8765 = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop |
                             Select-Object -ExpandProperty OwningProcess -Unique | Select-Object -First 1
        } catch { $portOwner8765 = $null }
        if ($portOwner8765) {
            $ownerCmd = ""
            try {
                $ownerCmd = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $portOwner8765) `
                             -ErrorAction Stop).CommandLine
            } catch { }
            Write-Host "[3/4] bridge: :8765 is HELD by pid $portOwner8765 but /conv does not answer." -ForegroundColor Yellow
            if ($ownerCmd) { Write-Host ("        " + (Hide-Secrets $ownerCmd)) -ForegroundColor Yellow }
            Write-Host "        Starting another bridge cannot bind that port. Stop pid $portOwner8765, then run this again." -ForegroundColor Yellow
            Write-Host "        (A bridge started outside .venv answers / but not /conv, which is what this looks like.)" -ForegroundColor Yellow
            $script:startupFailures += "bridge: :8765 held by pid $portOwner8765 and not serving /conv"
        } else {
        Write-Host "[3/4] bridge: starting (headless keepalive)"
        # ITS ERROR MESSAGE IS THE MOST USEFUL ONE IN THE WHOLE STARTUP and it was going
        # nowhere. The bridge exits with "No agent page. Set MCP_IMPL_AGENT_URL..." -- exactly
        # what the person needs -- into a hidden window that discards it. Redirected to a file
        # instead of un-hiding the window: this is a long-lived keepalive, and giving it a
        # console would leave a window that has to stay open for the app to work.
        $bridgeLog = Join-Path $script:diagDir "bridge.log"
        Start-Process powershell -WindowStyle Hidden -ArgumentList @(
            "-NoProfile","-ExecutionPolicy","Bypass","-File",
            ('"{0}"' -f (Join-Path $scriptDir "start_bridge.ps1")), "-Keepalive") `
            -RedirectStandardOutput $bridgeLog -RedirectStandardError "$bridgeLog.err"
        # DID IT SURVIVE? Fire-and-forget was asymmetric with the supervisor, the :8765-held
        # branch above, and the UI rebuild -- all of which are counted. The bridge exits at
        # once on "No agent page. Set MCP_IMPL_AGENT_URL...", most likely on a fresh PC where
        # that env var is unset, and the reason went only to $bridgeLog.err -- never to
        # startupFailures and never to the screen. HasExited cannot be the probe here: the
        # -Keepalive WRAPPER stays alive in its own loop and relaunches the dead child every
        # 3s, so the process is "still running" while nothing serves. /conv is the same
        # liveness signal this whole block already keys on, so that is what is checked. The
        # first-start Edge bring-up plus a Python import needs a few seconds before /conv can
        # answer, so this polls rather than probing once.
        $bridgeUp = $false
        for ($bi = 0; $bi -lt 8; $bi++) {
            Start-Sleep -Seconds 2
            if (Http-Up "http://127.0.0.1:8765/conv") { $bridgeUp = $true; break }
        }
        if (-not $bridgeUp) {
            $why = "bridge: started but :8765/conv did not answer within ~16s"
            try {
                if (Test-Path "$bridgeLog.err") {
                    $brLines = @(Get-Content "$bridgeLog.err" -Tail 5 -ErrorAction Stop |
                                 Where-Object { $_.Trim() })
                    if ($brLines.Count -gt 0) { $why = $why + ": " + ($brLines -join " / ") }
                }
            } catch { }
            Write-Host ("[3/4] " + $why) -ForegroundColor Yellow
            Write-Host "        (A fresh PC often has MCP_IMPL_AGENT_URL unset, which the bridge exits on -- see $bridgeLog.err.)" -ForegroundColor Yellow
            $script:startupFailures += $why
        }
        }
    }

    # 4) WPF apps. Launch only if not already running; build them first if an exe is missing,
    #    empty, or older than its sources (Invoke-UiStep). In -NoUi mode (used by logon
    #    autostart), keep the backend stack alive but leave the desktop untouched. Manual
    #    launchers still use the default behavior and open the apps.
    if ($NoUi) {
        Set-SplashStatus $script:splash "UI launch skipped for background startup..."
        Write-Host "[4/4] UI windows: skipped (-NoUi)"
    } else {
        Invoke-UiStep
    }

    Write-Host ""
    if ($NoUi) {
        Write-Host "Done. Background stack is up (bridge status: http://127.0.0.1:8765/status)."
        Write-Host "Open the full UI manually with: wscript.exe `"$root\scripts\start_all_hidden.vbs`""
    } else {
        Write-Host "Done. Chat: use the CopilotChat window. Fleet cockpit window is up."
        Write-Host "If a one-time M365 sign-in is needed, a visible Edge window will appear -- sign in there."
    }

    # 5) One-time convenience provisioning (Desktop icon + logon autostart). Runs last, after every
    #    service/UI above is already launched, so any failure here can never block real startup.
    Ensure-ConvenienceProvisioning

    if (-not $NoUi) {
        # Keep the splash up (BOUNDED ~20s) until a chat/cockpit window actually appears, then enforce
        # a minimum on-screen time so a fast (already-running) start is still seen, then let it close.
        Set-SplashStatus $script:splash "Almost ready..."
        for ($i = 0; $i -lt 40; $i++) {
            if (Get-Process CopilotChat, FleetCockpit -ErrorAction SilentlyContinue) { break }
            Pump-Splash $script:splash
            Start-Sleep -Milliseconds 500
        }
        if ($script:splash) {
            $rem = 2500 - ((Get-Date) - $script:splash.Start).TotalMilliseconds
            while ($rem -gt 0) { Pump-Splash $script:splash; Start-Sleep -Milliseconds 80; $rem -= 80 }
        }
    }
    Set-SplashStatus $script:splash "Ready."
    Start-Sleep -Milliseconds 500
}

# Drive startup. Prefer a MODAL splash (reliable display -- the SAME mechanism as the update
# dialog the user already sees) and run Invoke-Startup from a one-shot timer on its message loop,
# so the window stays visible the whole time. If the splash can't be built/shown, run startup
# directly so it is NEVER blocked.
$script:splash = $null
$script:startupFailures = @()
$script:lockTimedOut = $false           # Enter-StartAllLock gave up: nothing was started
$script:supervisorStartedHere = $false  # this run started a supervisor that survived
$script:supervisorDied = $false         # this run started one and it exited at once
$script:fleetLeftToSupervisor = $false  # resume and reap left to that supervisor

# ONE PLACE, INSIDE THE REPO. Diagnostics were scattered across %TEMP% and hidden windows, so
# the answer existed and could not be found. Everything that a hidden process would otherwise
# swallow is written here, next to the thing it is about.
$script:diagDir = Join-Path $root ".setup\logs"
try { if (-not (Test-Path $script:diagDir)) { New-Item -ItemType Directory -Force $script:diagDir | Out-Null } } catch { }

function Hide-Secrets([string]$text) {
    # CENTRAL REDACTION. A log that is finally visible is also a log that can be pasted into a
    # chat window, and this stack handles a Bearer token, an unlock password and a tunnel URL
    # that is effectively a capability. Redacted once, here, rather than remembered at each
    # site that writes.
    if (-not $text) { return $text }
    $t = $text
    $t = [regex]::Replace($t, '(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}', '$1<redacted>')
    $t = [regex]::Replace($t, '(?i)(MCP_API_KEY\s*=\s*)\S+', '$1<redacted>')
    $t = [regex]::Replace($t, '(?i)(MCP_UNLOCK_PASSWORD[A-Z_]*\s*=\s*)\S+', '$1<redacted>')
    $t = [regex]::Replace($t, '(?i)(password|passwd|secret|token)(["'':\s=]+)\S+', '$1$2<redacted>')
    $t = [regex]::Replace($t, 'https://[A-Za-z0-9\-]+\.devtunnels\.ms\S*', 'https://<tunnel>.devtunnels.ms/...')
    return $t
}
$ranViaSplash = $false
try {
    if (-not $NoSplash) { $script:splash = Start-Splash }
    if ((-not $NoSplash) -and $script:splash -and $script:splash.Form) {
        $script:splash.Start = (Get-Date)
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 80
        $timer.Add_Tick({
            $timer.Stop()
            # THE EXCEPTION USED TO VANISH HERE. `catch { }` on the entire startup meant a
            # failure anywhere in it left no trace at all: no message, no code, nothing to
            # read. The splash closed and everything looked finished.
            try { Invoke-Startup } catch { $script:startupFailures += "startup: $($_.Exception.Message)" }
            try { $script:splash.Form.Close() } catch { }
        })
        $timer.Start()
        [void]$script:splash.Form.ShowDialog()
        try { $script:splash.Form.Dispose() } catch { }
        $ranViaSplash = $true
    }
} catch { $ranViaSplash = $false }
if (-not $ranViaSplash) {
    $script:splash = $null
    try { Invoke-Startup } catch { $script:startupFailures += "startup: $($_.Exception.Message)" }
}

# THE TUNNEL, CHECKED AND COUNTED (Test-TunnelServing; new-PC analysis D24). After the splash
# has closed, because it may wait up to a minute for a supervisor started seconds ago to host;
# and under -CoreOnly too, which is the start whose exit code quickstart reads before STEP 5
# asks for a Copilot Studio connection test. Not after a lock timeout: nothing was started.
if (-not $script:lockTimedOut) {
    try {
        if ($script:supervisorDied) {
            Write-Host "[tunnel] not checked: the supervisor, which hosts it, did not start (see above)" -ForegroundColor DarkGray
        } else {
            Test-TunnelServing
        }
    } catch {
        Write-Host ("[tunnel] check skipped (" + $_.Exception.Message + ")") -ForegroundColor DarkGray
    }
}

# THE SESSION EXPIRES AND NOTHING LOOKED. ensure_m365_signin is called from quickstart once, at
# install time, and by doctor with --check-only as an [INFO] that counts toward nothing. The
# launcher that runs every day -- the Desktop shortcut, the logon task -- did not call it at all,
# so an expired M365 session produced a completely green startup and an agent that silently could
# not work. Handing the operator a script to run by hand is not the fix; the launcher carries it.
#
# A background logon start never surfaces UI, which is the rule this file already follows, so it
# asks and records. A manual start brings the window forward, bounded at 180s rather than the
# helper's 600s default, because a launcher that can block for ten minutes is its own fault.
#
# "could not tell" stays silent in both: the fleet is websocket-driven and opens no tabs, so a
# signed-in machine looks identical to one with no tab, and reporting that would send somebody to
# fix what is not broken.
function Report-OtherProfileSignIns {
    <#
      EVERY MANAGED EDGE, NOT JUST :9222 -- and SAY SO, do not fix it here.

      Each managed browser runs on its own --user-data-dir (Edge locks a profile to one
      process, so running them at once REQUIRES distinct profiles), and a distinct profile is a
      distinct cookie jar. Signing in on the companion signs in the companion. The owner asked
      whether a sign-in could have landed in one of the two browsers and not the other, on
      2026-09-23: structurally, yes, and until now nothing looked.

      REPORTED, NOT ESCALATED. Surfacing a second browser to sign it in is exactly what the
      comment above spent 751 MB learning not to do at startup. A line the operator can act on
      is the whole job here; doctor carries the same rows with the command.

      "cannot_tell" stays silent, for the reason this file already gives: a healthy machine has
      no M365 tab, so that is the normal answer and not evidence of anything.
    #>
    param([string] $Report)
    if (-not $Report) { return }
    foreach ($line in ($Report -split "`r?`n")) {
        $m = [regex]::Match($line, '^\s*PROFILE:\s+(\d+)\s+(\S+)\s+(\w+)(\s+\[primary\])?\s*\((.*)\)\s*$')
        if (-not $m.Success) { continue }
        if ($m.Groups[4].Success -and $m.Groups[4].Value) { continue }
        if ($m.Groups[3].Value -ne "sign_in_needed") { continue }
        Write-Host ("[m365] {0} (:{1}) is on a sign-in page -- it is a SEPARATE browser profile, so the companion's sign-in does not cover it. {2}" `
            -f $m.Groups[2].Value, $m.Groups[1].Value, $m.Groups[5].Value) -ForegroundColor Yellow
        $script:startupFailures += ("M365 sign-in needed on " + $m.Groups[2].Value + " (:" + $m.Groups[1].Value + ")")
    }
}

try {
    $signinPs = Join-Path $scriptDir "ensure_m365_signin.ps1"
    if ((Test-Path $signinPs) -and (-not $CoreOnly) -and (-not $script:lockTimedOut)) {
        if ($NoUi) {
            # THE PYTHON DIRECTLY, not the wrapper. ensure_m365_signin.ps1 ends with an
            # unconditional `exit 0` -- deliberately, so a missing sign-in never fails the
            # whole setup -- which means a check of its exit code can never fire. Its exit
            # codes are 0 signed in / 1 a sign-in wall is open / 2 could not tell.
            $signinPy = Join-Path $scriptDir "ensure_m365_signin.py"
            $pyExe = Join-Path $root ".venv\Scripts\python.exe"
            $signinReport = ""
            if ((Test-Path $signinPy) -and (Test-Path $pyExe)) {
                $signinReport = (& $pyExe $signinPy --port 9222 --check-only --all 2>&1 | Out-String)
            } else {
                $global:LASTEXITCODE = 2
            }
            if ($LASTEXITCODE -eq 1) {
                Write-Host "[m365] a sign-in is needed, and a background start cannot show it." -ForegroundColor Yellow
                $script:startupFailures += "M365 sign-in needed (background start could not prompt)"
            }
            Report-OtherProfileSignIns $signinReport
        } else {
            # ASK FIRST; SURFACE ONLY ON A REAL WALL. Running the full helper here opened a
            # HEADED companion Edge on m365.cloud.microsoft/chat and left it running: measured
            # 751 MB across ten processes, a taskbar button, and a window sitting 30px onto the
            # screen -- on a machine that was already signed in. The helper surfaces whenever it
            # cannot tell, and "cannot tell" is the NORMAL answer here: the fleet is
            # websocket-driven and keeps zero tabs, so a healthy machine has no M365 tab to judge
            # from. The launcher was therefore asking a question whose usual answer costs three
            # quarters of a gigabyte and a window nobody asked for.
            #
            # --check-only answers without taking the window. Exit 1 -- a sign-in wall is
            # actually open -- is the only positive evidence of "not signed in", and only that
            # escalates to the helper that shows it. 2 (could not tell) stays silent, exactly as
            # it already does on the -NoUi path above.
            $signinPy = Join-Path $scriptDir "ensure_m365_signin.py"
            $pyExe = Join-Path $root ".venv\Scripts\python.exe"
            $signinReport = ""
            if ((Test-Path $signinPy) -and (Test-Path $pyExe)) {
                $signinReport = (& $pyExe $signinPy --port 9222 --check-only --all 2>&1 | Out-String)
            } else {
                $global:LASTEXITCODE = 2
            }
            if ($LASTEXITCODE -eq 1) {
                & powershell -NoProfile -ExecutionPolicy Bypass -File ('"{0}"' -f $signinPs) -TimeoutSeconds 180 | Out-Null
            }
            Report-OtherProfileSignIns $signinReport
        }
    }
} catch {
    Write-Host ("[m365] sign-in check skipped (" + $_.Exception.Message + ")") -ForegroundColor DarkGray
}

# WHAT WENT WRONG, SAID OUT LOUD AT THE END. This script returns 0 whatever happens, so a
# caller checking its exit code learns nothing -- the missing thing was never the code, it was
# any statement of the failure. Printed last so it is the final thing on screen rather than
# something that scrolled past during a two-minute startup. AFTER the tunnel and sign-in
# checks: it used to be printed before the sign-in check, so a failure that check recorded
# was in the exit code and missing from the list.
if ($script:startupFailures.Count -gt 0) {
    Write-Host ""
    Write-Host "=========================================================" -ForegroundColor Yellow
    Write-Host " Startup finished with $($script:startupFailures.Count) problem(s)" -ForegroundColor Yellow
    Write-Host "=========================================================" -ForegroundColor Yellow
    foreach ($f in $script:startupFailures) { Write-Host ("  - " + (Hide-Secrets $f)) -ForegroundColor Yellow }
    Write-Host "  Run doctor.bat for the specific fix for each line." -ForegroundColor Yellow
    Write-Host ""
}

# AND WHERE SOMEBODY WILL SEE IT when this ran hidden -- see Write-StartupSummary's header.
$startMode = Get-StartAllMode
$summaryPath = Join-Path $script:diagDir "start_all_summary.txt"
$summaryWritten = Write-StartupSummary $summaryPath @($script:startupFailures) $startMode
# WHO STARTED IT AND HOW IT ENDED (see Get-LaunchLineage), while the lock is still held.
$runOutcome = $(if ($script:lockTimedOut) { "lock timed out" } elseif ($script:startupFailures.Count -gt 0) { "failures" } else { "ok" })
$null = Write-StartAllRunRecord (Join-Path $script:diagDir "start_all_runs.jsonl") (New-StartAllRunRecord $runOutcome)
if ($summaryWritten -and (Test-ShouldNotifyStartupFailures $script:startupFailures.Count ([bool]$NoUi) (Test-ConsoleVisible))) {
    Send-StartupFailureNotice $summaryPath | Out-Null
}
Exit-StartAllLock

# WHERE TO LOOK, PRINTED EVERY TIME. Named whether or not anything failed, because the case
# that needs it most -- "the chat window does not respond" -- produces no error here at all:
# the bridge failed silently in a hidden process, and until now its message went nowhere.
Write-Host ("  Logs: " + $script:diagDir) -ForegroundColor DarkGray
Write-Host ("        supervisor: " + (Join-Path $env:TEMP 'm365-companion-supervisor.log')) -ForegroundColor DarkGray
Write-Host ("        this start: " + $summaryPath) -ForegroundColor DarkGray

# THE COUNT IS THE EXIT CODE, the same convention doctor uses. Until now this script reported
# its problems in prose and exited 0 regardless, so a caller could not tell a startup that
# failed from one that worked -- and quickstart, which launches it twice, went on to the manual
# Copilot Studio step after a launch that had not happened. Nothing read this code before
# (quickstart ignored it; start_all.bat runs the VBS without checking; the Desktop launcher and
# the logon task do not look either), so giving it meaning cannot break anything that works.
exit $script:startupFailures.Count
