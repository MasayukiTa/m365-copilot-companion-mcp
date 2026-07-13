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

function Ensure-EnvDefaults {
    # Release upgrades must not overwrite a user's .env, but existing installations need new
    # safe defaults. Append MCP_REVIEW_P2C=0 only when the key is absent; an explicit 1 is kept.
    try {
        $path = Join-Path $root ".env"
        if (-not (Test-Path $path)) { return }
        $text = [System.IO.File]::ReadAllText($path)
        if ($text -match '(?m)^\s*MCP_REVIEW_P2C\s*=') { return }
        if ($text.Length -gt 0 -and -not ($text.EndsWith("`n") -or $text.EndsWith("`r"))) {
            $text += "`r`n"
        }
        $text += "MCP_REVIEW_P2C=0`r`n"
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
    } catch {
        # A default-backfill failure must never prevent daily startup.
    }
}

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
        $f.ControlBox = $false
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

        # 3) Fetch with a hard timeout via a background job. Offline/auth/slow -> give up quietly.
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
            try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
            try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
            return
        }
        $fetchExit = Receive-Job $job
        try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
        if ($fetchExit -ne 0) { return }

        # 4) How many commits behind upstream? Upstream unset -> fails -> return.
        $behindRaw = & git -C $root rev-list --count "HEAD..@{u}" 2>$null
        if ($LASTEXITCODE -ne 0) { return }
        $behind = 0
        if (-not [int]::TryParse(($behindRaw | Select-Object -First 1), [ref]$behind)) { return }
        if ($behind -le 0) { return }   # already up to date -> no dialog

        # 5) Ask the user (visible even though the host process is hidden). Phrase the count as
        #    "version(s)", NOT "commit(s)" -- commit jargon does not communicate to a general user.
        $title = "M365 Companion - Update available"
        $verWord = "versions"
        if ($behind -eq 1) { $verWord = "version" }
        $body  = "Your copy is {0} {1} behind the latest.`n`nUpdate to the latest now?" -f $behind, $verWord
        $answer = Show-OwnedDialog $body $title "YesNo" "Information"
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }

        # 6) Pull fast-forward only. Keep the dialog jargon-free: no raw git output (it can carry
        #    non-ASCII commit text and only confuses a general user).
        & git -C $root pull --ff-only 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Show-OwnedDialog "Update could not complete. Your current version is kept." $title "OK" "Warning" | Out-Null
            return
        }

        # 7b) If the pull changed any ui/*.cs, rebuild the UI exes (non-fatal if it fails).
        $rebuildNote = ""
        try {
            $changed = & git -C $root diff --name-only "HEAD@{1}" HEAD 2>$null
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

        Show-OwnedDialog ("Updated to the latest version.{0}" -f $rebuildNote) $title "OK" "Information" | Out-Null

        # 8) DESIGN NOTE: the pull above just landed new files on disk, but THIS process
        #    is still running the OLD (pre-pull) start_all.ps1 that was already loaded
        #    into memory when it started -- without re-exec, none of the freshly-pulled
        #    code (this very fix included, e.g. tunnel self-heal wiring) takes effect
        #    until a SECOND run. Fix: re-launch the just-pulled script now, the same
        #    hidden way the .vbs launcher does, preserving the original switches, and
        #    let the FRESH process finish this startup (heal + stack) with the pulled
        #    code; THIS process then exits so only the fresh instance continues. Never
        #    lets a re-exec failure stop startup: any error here is swallowed and this
        #    (old) process simply falls through and keeps going on its own.
        $guardAlreadySet = ($env:MCP_STARTALL_REEXEC -eq "1")
        if (Test-ShouldReExecAfterUpdate -GuardAlreadySet $guardAlreadySet -Behind $behind -PullSucceeded $true) {
            try {
                $selfPath = Join-Path $scriptDir "start_all.ps1"
                if (Test-Path $selfPath) {
                    $reArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $selfPath)
                    if ($NoUi) { $reArgs += "-NoUi" }
                    if ($NoSplash) { $reArgs += "-NoSplash" }
                    $env:MCP_STARTALL_REEXEC = "1"
                    Start-Process powershell -WindowStyle Hidden -ArgumentList $reArgs | Out-Null
                    exit
                }
            } catch {
                # Re-exec is best-effort only -- fall through and let this (old)
                # process finish the current startup rather than leaving nothing
                # running.
            }
        }
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
    try {
        Start-Process powershell -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $cfgScript
        ) -WorkingDirectory $root -Wait
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
        if (Test-Path $markerPath) {
            # Already provisioned once -- never touch it again, even if the user removed
            # the icon or the autostart shortcut since.
            return
        }
        if (-not (Test-Path $setupDir)) {
            New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
        }

        # a) Desktop shortcut. make_desktop_shortcut.ps1 is idempotent (overwrites), so running
        #    it here is harmless even on a machine where it was already created by hand/quickstart.
        $shortcutScript = Join-Path $scriptDir "make_desktop_shortcut.ps1"
        if (Test-Path $shortcutScript) {
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
        if (Test-Path $autostartScript) {
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
        try {
            "provisioned" | Out-File -FilePath $markerPath -Encoding ascii -Force
        } catch { }
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
    Set-SplashStatus $script:splash "Starting the MCP server and Dev Tunnel..."
    if (Proc-Running 'supervisor\.ps1') {
        Write-Host "[1/4] supervisor (MCP server + tunnel): already running -- left as-is"
    } else {
        $tn = Env-Value "MCP_TUNNEL_NAME"
        $supArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File","$scriptDir\supervisor.ps1")
        if ($tn) { $supArgs += @("-TunnelName", $tn); Write-Host "[1/4] supervisor (MCP server + tunnel '$tn'): starting" }
        else     { Write-Host "[1/4] supervisor (MCP server + tunnel): starting" }
        Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs
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
    Set-SplashStatus $script:splash "Starting the chat bridge..."
    if (Proc-Running 'start_bridge\.ps1') {
        Write-Host "[3/4] bridge keepalive: already running"
    } elseif (Http-Up "http://127.0.0.1:8765/conv") {
        Write-Host "[3/4] bridge :8765: already serving (no keepalive supervisor, but up)"
    } else {
        Write-Host "[3/4] bridge: starting (headless keepalive)"
        Start-Process powershell -WindowStyle Hidden -ArgumentList @(
            "-NoProfile","-ExecutionPolicy","Bypass","-File","$scriptDir\start_bridge.ps1","-Keepalive")
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
$ranViaSplash = $false
try {
    if (-not $NoSplash) { $script:splash = Start-Splash }
    if ((-not $NoSplash) -and $script:splash -and $script:splash.Form) {
        $script:splash.Start = (Get-Date)
        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 80
        $timer.Add_Tick({
            $timer.Stop()
            try { Invoke-Startup } catch { }
            try { $script:splash.Form.Close() } catch { }
        })
        $timer.Start()
        [void]$script:splash.Form.ShowDialog()
        try { $script:splash.Form.Dispose() } catch { }
        $ranViaSplash = $true
    }
} catch { $ranViaSplash = $false }
if (-not $ranViaSplash) { $script:splash = $null; Invoke-Startup }
