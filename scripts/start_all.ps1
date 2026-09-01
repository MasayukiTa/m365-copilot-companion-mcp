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
    [switch]$NoSplash
)

$ErrorActionPreference = "Continue"
# This script lives in <repo>\scripts. $root is the REPO ROOT (.env, .git, ui\ live there);
# $scriptDir is the scripts dir where the sibling launchers (supervisor.ps1,
# start_companion_edge.ps1, start_bridge.ps1) now live.
$scriptDir = $PSScriptRoot
$root = Split-Path -Parent $scriptDir

# Shared PURE helpers (Get-SupervisorArgTunnel / Get-BareTunnelName /
# Test-SupervisorTunnelDrift) for detecting a supervisor that drifted onto a
# stale/borrowed tunnel -- see tunnel_name_util.ps1's header comment. No
# top-level side effects, so dot-sourcing it here is safe.
. (Join-Path $scriptDir "tunnel_name_util.ps1")
. (Join-Path $scriptDir "update_recovery.ps1")

# The .env backfill lives in one testable place; see scripts/win/env_defaults.ps1 for why
# deciding "is this value ours or the user's" needs a record of what we wrote.
. (Join-Path $PSScriptRoot "win\env_defaults.ps1")
Ensure-EnvDefaults

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
function Proc-Is-Outdated([string]$repo, [string]$match, [string[]]$dirs, [string[]]$files) {
    # True when a process matching $match is running that started BEFORE the newest source
    # it loads. Only the modules that process imports at startup are considered, so editing
    # docs or an unrelated tool never forces a restart. Any failure answers $false: an
    # unreadable timestamp must never be the reason a healthy process gets torn down.
    try {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                   Where-Object { $_.CommandLine -and ($_.CommandLine -match $match) })
        if ($procs.Count -eq 0) { return $false }   # nothing running -> normal start path
        $started = ($procs | Measure-Object -Property CreationDate -Minimum).Minimum
        if (-not $started) { return $false }
        $newest = $null
        foreach ($sub in $dirs) {
            $d = Join-Path $repo $sub
            if (-not (Test-Path $d)) { continue }
            $m = (Get-ChildItem $d -Filter *.py -File -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -notlike 'test_*' } |
                  Measure-Object -Property LastWriteTime -Maximum).Maximum
            if ($m -and (-not $newest -or $m -gt $newest)) { $newest = $m }
        }
        foreach ($f in $files) {
            $p = Join-Path $repo $f
            if (-not (Test-Path $p)) { continue }
            $m = (Get-Item $p -ErrorAction SilentlyContinue).LastWriteTime
            if ($m -and (-not $newest -or $m -gt $newest)) { $newest = $m }
        }
        if (-not $newest) { return $false }
        return ($newest -gt $started)
    } catch { return $false }
}
function Bridge-Is-Outdated([string]$repo) {
    return (Proc-Is-Outdated $repo 'copilot_bridge\.py' @('bridge', 'tools', 'relay') @())
}
function Server-Is-Outdated([string]$repo) {
    # main.py そのものと、それが起動時に取り込む tools/relay を見る。ブリッジだけを
    # 見ていた頃、main.py の説明文を書き換えても再起動されず、古い文言が配られ続けた。
    # 直したのに直っていない、という一番たちの悪い状態になる。
    return (Proc-Is-Outdated $repo 'main\.py' @('tools', 'relay') @('main.py'))
}
function Stop-Bridge-Processes() {
    # Take the keepalive supervisor down first, otherwise it just respawns the python we are
    # about to stop and the restart silently does nothing.
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match 'start_bridge\.ps1') } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match 'copilot_bridge\.py') } |
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
# startup is NEVER blocked. No X (can't be closed onto a half-started stack), and a minimum
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
        $f.MinimizeBox = $false
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
                $reArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $selfPath)
                if ($NoUi) { $reArgs += "-NoUi" }
                if ($NoSplash) { $reArgs += "-NoSplash" }
                $env:MCP_STARTALL_REEXEC = "1"
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
# One-time convenience provisioning: a person who downloads the repo, manually finishes
# devtunnel + agent-URL setup, then runs start_all expects it to also finish the two other
# one-time setup steps -- the Desktop icon (make_desktop_shortcut.ps1) and the logon autostart
# registration (register-supervisor.ps1). Neither happens today via this path (the shortcut
# script is only invoked from quickstart.bat behind a Y/N prompt; autostart registration is a
# purely manual step), so provision both here, but ONLY ONCE EVER: gated on a marker file so
# that if the user later deletes the icon or unregisters autostart, start_all does not fight
# them by silently recreating it on the next run. Runs AFTER the services/UIs are brought up so
# a failure here can never block or delay the actual startup.
# ---------------------------------------------------------------------------
function Ensure-ConvenienceProvisioning {
    try {
        $setupDir = Join-Path $root ".setup"
        $markerPath = Join-Path $setupDir "convenience_provisioned"
        # THE MARKER RECORDS A DECISION, NOT AN ACT -- and its ABSENCE is not consent.
        #
        # This used to provision both whenever the marker was missing, so a machine that had
        # never been asked got a Desktop shortcut and a logon autostart entry anyway. Worse,
        # quickstart.bat asked "Create a launcher? [Y/n]" AFTERWARDS, so the question was put
        # to someone whose answer could no longer matter: saying no changed nothing, and the
        # autostart registration was never mentioned at all. Both are changes OUTSIDE this
        # folder, and both persist after the repo is deleted.
        #
        # quickstart.bat now asks first and writes the answers here as
        #   shortcut=yes|no
        #   autostart=yes|no
        # With no file there is no decision, and with no decision nothing is created.
        if (-not (Test-Path $markerPath)) {
            Write-Host "[provision] no consent on record -- creating nothing."
            Write-Host "[provision] run quickstart.bat to be asked, or scripts\make_desktop_shortcut.ps1"
            Write-Host "[provision] and scriptsegister-supervisor.ps1 to do either by hand."
            return
        }

        $decision = @{}
        foreach ($line in (Get-Content $markerPath -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*([A-Za-z_]+)\s*=\s*(\S+)\s*$') { $decision[$matches[1]] = $matches[2] }
        }
        # An older marker holds the single word "provisioned": that machine was already
        # provisioned under the previous behaviour, so re-doing it would fight the user. Treat
        # it as "both already handled, touch nothing" rather than re-asking or re-creating.
        if ($decision.Count -eq 0) { return }

        $wantShortcut  = ($decision['shortcut']  -eq 'yes')
        $wantAutostart = ($decision['autostart'] -eq 'yes')
        if (-not $wantShortcut -and -not $wantAutostart) { return }

        if (-not (Test-Path $setupDir)) {
            New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
        }

        # a) Desktop shortcut. make_desktop_shortcut.ps1 is idempotent (overwrites), so running
        #    it here is harmless even on a machine where it was already created by hand/quickstart.
        $shortcutScript = Join-Path $scriptDir "make_desktop_shortcut.ps1"
        if ($wantShortcut -and (Test-Path $shortcutScript)) {
            try {
                Start-Process powershell -WindowStyle Hidden -Wait -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $shortcutScript
                ) -WorkingDirectory $root
                Write-Host "[provision] desktop shortcut created"
            } catch {
                Write-Host "[provision] desktop shortcut skipped: $_"
            }
        }

        # b) Logon autostart. register-supervisor.ps1 is idempotent (re-creates the same Startup-
        #    folder shortcut), so running it here is harmless even on an already-registered machine.
        $autostartScript = Join-Path $scriptDir "register-supervisor.ps1"
        if ($wantAutostart -and (Test-Path $autostartScript)) {
            try {
                Start-Process powershell -WindowStyle Hidden -Wait -ArgumentList @(
                    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $autostartScript
                ) -WorkingDirectory $root
                Write-Host "[provision] logon autostart registered"
            } catch {
                Write-Host "[provision] autostart registration skipped: $_"
            }
        }

        # Write the marker AFTER attempting both, regardless of whether either one succeeded --
        # this is a single best-effort attempt, not a retry-every-startup nag. No personal path or
        # username is recorded, just a generic tag.
        # NOT overwritten here any more: this file is the record of what the person chose,
        # and stamping it with "provisioned" would erase that answer.
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

# Everything that brings the stack up, as ONE function so it can run either INSIDE the splash's
# message loop (a one-shot timer, so the modal splash stays visible while this runs) OR directly
# as a fallback if the splash cannot be shown. Status updates target $script:splash (no-op if null).
function Invoke-Startup {
    Start-BackgroundSecurityUiCloser

    # First-time setup gate (BUG 3a): must run before anything is started, and before the
    # update-check/splash sequence below so its status text is not overwritten mid-prompt.
    # Background logon startup must never show interactive setup; a manual launch still does.
    if ($NoUi) {
        Write-Host "[setup] first-time interactive setup skipped (-NoUi)"
    } else {
        Invoke-FirstTimeSetupGate
    }

    # Pre-flight update check (best-effort, non-blocking). Runs once before any service starts.
    # In background logon startup this is skipped because update prompts are visible dialogs.
    if ($NoUi) {
        Write-Host "[update] update check skipped (-NoUi)"
    } else {
        Set-SplashStatus $script:splash "Checking for updates..."
        Check-ForUpdates
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
        $supArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File","$scriptDir\supervisor.ps1")
        if ($tn) { $supArgs += @("-TunnelName", $tn); Write-Host "[1/4] supervisor (MCP server + tunnel '$tn'): starting" }
        else     { Write-Host "[1/4] supervisor (MCP server + tunnel): starting" }
        Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs
    }
    function Get-RunningSupervisorProcesses {
        # All Win32_Process entries whose command line launches supervisor.ps1. Normally
        # zero or one; returned as an array so a drift-restart can stop every match.
        try {
            return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                      Where-Object { $_.CommandLine -and ($_.CommandLine -match 'supervisor\.ps1') })
        } catch { return @() }
    }
    # main.py が自分のソースより古ければ落とす。supervisor が居れば数十秒で拾い直し、
    # 居なければ下の起動経路が立ち上げる。トンネルには触らない。
    # ここが無かった頃、main.py を直しても古いプロセスが残り、直したはずの説明文が
    # 配られ続けた（直っていないのか反映されていないのかが切り分けられない）。
    if (Server-Is-Outdated $root) {
        Write-Host "[1/4] MCP server: code is newer than the running process -- restarting"
        try {
            # SCOPED TO THIS CHECKOUT. Matching 'main.py' alone kills ANY process whose
            # command line contains it -- another clone of this repo on the same machine, an
            # unrelated project's main.py, an editor running one under a debugger. supervisor.ps1
            # already gets this right with the same idiom; this copy did not, so one file in the
            # repository was correct and the other was not.
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and ($_.CommandLine -match 'main\.py') -and ($_.CommandLine -like "*$root*") } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        } catch { }
        Start-Sleep -Seconds 2
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
    if (Test-Path $py) {
        $resumer = Join-Path $root "scripts\win\resume_interrupted_fleet.py"
        if (Test-Path $resumer) {
            try {
                & $py $resumer --resume 2>&1 | ForEach-Object { Write-Host "      $_" }
            } catch { Write-Host "      (resume check skipped: $($_.Exception.Message))" }
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
    if (Test-Path $py) {
        try {
            & $py -m relay.fleet_reaper --reap 2>&1 | ForEach-Object { Write-Host "      $_" }
        } catch { Write-Host "      (phantom-run sweep skipped: $($_.Exception.Message))" }
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
    Set-SplashStatus $script:splash "Starting the chat bridge..."
    if (Bridge-Is-Outdated $root) {
        Write-Host "[3/4] bridge: code is newer than the running process -- restarting"
        Stop-Bridge-Processes
        Start-Sleep -Seconds 2
    }
    if (Proc-Running 'start_bridge\.ps1') {
        Write-Host "[3/4] bridge keepalive: already running"
    } elseif (Http-Up "http://127.0.0.1:8765/conv") {
        Write-Host "[3/4] bridge :8765: already serving (no keepalive supervisor, but up)"
    } else {
        Write-Host "[3/4] bridge: starting (headless keepalive)"
        # ITS ERROR MESSAGE IS THE MOST USEFUL ONE IN THE WHOLE STARTUP and it was going
        # nowhere. The bridge exits with "No agent page. Set MCP_IMPL_AGENT_URL..." -- exactly
        # what the person needs -- into a hidden window that discards it. Redirected to a file
        # instead of un-hiding the window: this is a long-lived keepalive, and giving it a
        # console would leave a window that has to stay open for the app to work.
        $bridgeLog = Join-Path $script:diagDir "bridge.log"
        Start-Process powershell -WindowStyle Hidden -ArgumentList @(
            "-NoProfile","-ExecutionPolicy","Bypass","-File","$scriptDir\start_bridge.ps1","-Keepalive") `
            -RedirectStandardOutput $bridgeLog -RedirectStandardError "$bridgeLog.err"
    }

    # 4) WPF apps. Launch only if not already running; build them first if the exe is missing.
    #    In -NoUi mode (used by logon autostart), keep the backend stack alive but leave the
    #    desktop untouched. Manual launchers still use the default behavior and open the apps.
    if ($NoUi) {
        Set-SplashStatus $script:splash "UI launch skipped for background startup..."
        Write-Host "[4/4] UI windows: skipped (-NoUi)"
    } else {
        # rebuild_ui.ps1 builds AND relaunches BOTH apps itself, so if either exe is missing we
        # invoke it exactly once for the whole loop ($rebuilt flag) rather than once per app.
        Set-SplashStatus $script:splash "Opening the chat and cockpit windows..."
        $rebuilt = $false
        foreach ($app in @("CopilotChat","FleetCockpit")) {
            if (Get-Process $app -ErrorAction SilentlyContinue) {
                Write-Host "[4/4] ${app}: already running"
            } elseif (Test-Path "$root\ui\$app.exe") {
                Write-Host "[4/4] ${app}: launching"
                Start-Process "$root\ui\$app.exe"
            } elseif ($rebuilt) {
                # Already tried a rebuild this loop (below) -- re-check post-rebuild state without
                # rebuilding again.
                if (Get-Process $app -ErrorAction SilentlyContinue) {
                    Write-Host "[4/4] ${app}: launched by rebuild"
                } else {
                    Write-Host "[4/4] ${app}: still not running after rebuild -- see docs\TROUBLESHOOTING.md"
                }
            } else {
                $rebuildScript = Join-Path $root "ui\rebuild_ui.ps1"
                if (Test-Path $rebuildScript) {
                    Write-Host "[4/4] $app.exe not built yet -- building both UI apps (first run, ~30s)..."
                    Set-SplashStatus $script:splash "Building the chat and cockpit apps (first run, ~30s)..."
                    $rebuilt = $true
                    try {
                        & $rebuildScript | Out-Null
                        if (Get-Process $app -ErrorAction SilentlyContinue) {
                            Write-Host "[4/4] ${app}: built and launched"
                        } elseif (Test-Path "$root\ui\$app.exe") {
                            Write-Host "[4/4] ${app}: built -- launching"
                            Start-Process "$root\ui\$app.exe"
                        } else {
                            Write-Host "[4/4] ${app}: rebuild ran but exe still missing -- see docs\TROUBLESHOOTING.md ('csc.exe not found' row)"
                        }
                    } catch {
                        Write-Host "[4/4] ${app}: rebuild failed ($_) -- see docs\TROUBLESHOOTING.md ('csc.exe not found' row)"
                    }
                } else {
                    Write-Host "[4/4] $app.exe not built yet, and ui\rebuild_ui.ps1 is missing -- see docs\TROUBLESHOOTING.md"
                }
            }
        }
    }

    Write-Host ""
    if ($NoUi) {
        Write-Host "Done. Background stack is up. Chat bridge: http://127.0.0.1:8765"
        Write-Host "Open the full UI manually with: wscript.exe `"$root\scripts\start_all_hidden.vbs`""
    } else {
        Write-Host "Done. Chat UI: http://127.0.0.1:8765 (or the CopilotChat window). Fleet cockpit window is up."
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

# WHAT WENT WRONG, SAID OUT LOUD AT THE END. This script returns 0 whatever happens, so a
# caller checking its exit code learns nothing -- the missing thing was never the code, it was
# any statement of the failure. Printed last so it is the final thing on screen rather than
# something that scrolled past during a two-minute startup.
if ($script:startupFailures.Count -gt 0) {
    Write-Host ""
    Write-Host "=========================================================" -ForegroundColor Yellow
    Write-Host " Startup finished with $($script:startupFailures.Count) problem(s)" -ForegroundColor Yellow
    Write-Host "=========================================================" -ForegroundColor Yellow
    foreach ($f in $script:startupFailures) { Write-Host ("  - " + (Hide-Secrets $f)) -ForegroundColor Yellow }
    Write-Host "  Run doctor.bat for the specific fix for each line." -ForegroundColor Yellow
    Write-Host ""
}

# WHERE TO LOOK, PRINTED EVERY TIME. Named whether or not anything failed, because the case
# that needs it most -- "the chat window does not respond" -- produces no error here at all:
# the bridge failed silently in a hidden process, and until now its message went nowhere.
Write-Host ("  Logs: " + $script:diagDir) -ForegroundColor DarkGray
Write-Host ("        supervisor: " + (Join-Path $env:TEMP 'm365-companion-supervisor.log')) -ForegroundColor DarkGray
