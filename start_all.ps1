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
        $f.Show()
        $f.Activate()
        $f.BringToFront()
        [System.Windows.Forms.Application]::DoEvents()
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
function Stop-Splash($splash) {
    try {
        if ($splash -and $splash.Form) {
            # Keep it on screen a minimum ~2.5s so a fast (already-running) startup is still seen.
            $remain = 2500 - ((Get-Date) - $splash.Start).TotalMilliseconds
            while ($remain -gt 0) {
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 80
                $remain -= 80
            }
            $splash.Form.Close()
            $splash.Form.Dispose()
            [System.Windows.Forms.Application]::DoEvents()
        }
    } catch { }
}

function Check-ForUpdates {
    # Non-fatal pre-flight: if the local checkout is behind the remote, offer to update.
    # Any failure (no git, no upstream, offline, auth needed, fetch timeout, pull fail)
    # is swallowed so daily startup is NEVER blocked. Runs once, before services start.
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
    } catch {
        # Update check is best-effort only; never block startup.
        return
    }
}

# Show the cold-start splash (best-effort) so the few-second wait has feedback, then run the
# update check with its status reflected on the splash.
$splash = Start-Splash

# Pre-flight update check (best-effort, non-blocking). Runs once before any service starts.
Set-SplashStatus $splash "Checking for updates..."
Check-ForUpdates

Write-Host "=== Daily startup (idempotent -- already-running parts are left as-is) ==="

# 1) Supervisor = MCP server + Dev Tunnel host. Its own global mutex makes a second instance exit
#    quietly, and it never touches a live `devtunnel host` -- so if a tunnel is already up, we keep
#    it. We still gate on the process so we don't spawn a doomed hidden window each run.
Set-SplashStatus $splash "Starting the MCP server and Dev Tunnel..."
if (Proc-Running 'supervisor\.ps1') {
    Write-Host "[1/4] supervisor (MCP server + tunnel): already running -- left as-is"
} else {
    # pass the tunnel name from .env (setup_devtunnel.ps1 writes MCP_TUNNEL_NAME) so a machine with
    # its own tunnel name hosts the right one instead of the hardcoded default.
    $tn = Env-Value "MCP_TUNNEL_NAME"
    $supArgs = @("-NoProfile","-ExecutionPolicy","Bypass","-File","$root\supervisor.ps1")
    if ($tn) { $supArgs += @("-TunnelName", $tn); Write-Host "[1/4] supervisor (MCP server + tunnel '$tn'): starting" }
    else     { Write-Host "[1/4] supervisor (MCP server + tunnel): starting" }
    Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs
}

# 2) Companion Edge :9222 (the fleet / agent Edge). Idempotent launcher; skip if the port answers.
Set-SplashStatus $splash "Starting the agent browser..."
if (Port-Up 9222) {
    Write-Host "[2/4] companion Edge :9222: already up"
} else {
    Write-Host "[2/4] companion Edge :9222: starting (headless)"
    try { & "$root\start_companion_edge.ps1" -Headless | Out-Null } catch { Write-Host "      (companion Edge launch returned: $_)" }
}

# 3) Bridge :9223 + chat backend (start_bridge -Keepalive). Skip if the keepalive supervisor is up.
Set-SplashStatus $splash "Starting the chat bridge..."
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
Set-SplashStatus $splash "Opening the chat and cockpit windows..."
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

# Bridge the rest of the cold-start gap: keep the splash up (BOUNDED) until a chat/cockpit
# window actually appears, so it closes when the UI is really ready -- not the instant the
# launchers fire. Capped at ~20s so it can never hang (e.g. exes not built yet).
Set-SplashStatus $splash "Almost ready..."
for ($i = 0; $i -lt 40; $i++) {
    if (Get-Process CopilotChat, FleetCockpit -ErrorAction SilentlyContinue) { break }
    Pump-Splash $splash
    Start-Sleep -Milliseconds 500
}
Set-SplashStatus $splash "Ready."
Start-Sleep -Milliseconds 800
Stop-Splash $splash
