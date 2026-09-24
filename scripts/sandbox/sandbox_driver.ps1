# sandbox_driver.ps1 -- runs INSIDE Windows Sandbox (LogonCommand), never on a real PC.
#
# Drives the install path unattended on a fresh Windows image and writes evidence to the
# mapped results folder: transcripts, exit codes, marker timings, state.json snapshots,
# a masked .env summary, screenshots and a summary.json, then DONE.json as the completion
# marker the host polls for. See scripts/sandbox/README.md.
#
# ASCII ONLY: Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page.
#
# Scenarios:
#   AC  quickstart.bat straight through (A), then start_all.bat daily start: once, again at
#       once, then ten clicks at the same moment (C).
#   B   quickstart.bat killed during the dependency install, killed again during .env
#       generation, then run again to resume.
#
# AS A STANDARD USER. Company users are not administrators, and the sandbox's own logon is. The
# admin phase (the LogonCommand) only creates a local user in Users (never Administrators),
# copies the tree where that user can read it, and starts the user phase AS THAT USER on this
# desktop (CreateProcessWithLogonW with the user's own profile and environment, as runas does).
# Every scenario runs in the user phase; its identity (whoami /groups) is recorded.
#
# THE ONLY THINGS LAUNCHED are quickstart.bat and start_all.bat, the way a person double-clicks
# them; answers reach quickstart only as typed console input (stdin). Nothing else in the tree
# (bootstrap.py, setup_devtunnel.ps1, start_all.ps1, doctor.ps1, ...) is run, and no state file
# is pre-created: a person cannot do either, so a result that depended on it would prove nothing.
# Everything else here only OBSERVES (files, logs, processes, ports, the screen) or kills the
# quickstart window for the interruption scenario.
param(
    [ValidateSet("admin", "user")][string]$Phase = "admin",
    [string]$Scenario = "AC",
    [string]$Harness = "C:\harness",
    [string]$Results = "C:\results",
    [int]$QuickstartCapSec = 1800,
    [int]$StartAllCapSec = 600
)

$ErrorActionPreference = "Continue"
$App = Join-Path $env:USERPROFILE "m365-copilot-companion"
$T0 = Get-Date
New-Item -ItemType Directory -Force -Path $Results | Out-Null

# The user phase's files are relayed into the same results folder, so the two phases write
# under different names.
$ProgressName = $(if ($Phase -eq "user") { "progress.log" } else { "admin_progress.log" })
$SummaryName = $(if ($Phase -eq "user") { "summary.json" } else { "admin_summary.json" })
function Log([string]$m) {
    $line = "{0:HH:mm:ss} [+{1,6:N0}s] {2}" -f (Get-Date), ((Get-Date) - $T0).TotalSeconds, $m
    try { Add-Content -LiteralPath (Join-Path $Results $ProgressName) -Value $line -Encoding ASCII } catch { }
}

$Summary = [ordered]@{
    scenario = $Scenario
    started  = (Get-Date).ToString("o")
    app_dir  = $App
    runs     = @()
}
function Save-Summary {
    try { $Summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $Results $SummaryName) -Encoding UTF8 } catch { Log "summary write failed: $_" }
}

try { Add-Type -AssemblyName System.Windows.Forms, System.Drawing } catch { }
function Save-Screen([string]$Name) {
    try {
        $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
        $bmp.Save((Join-Path $Results "$Name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose()
    } catch { Log "screenshot $Name failed: $_" }
    try {
        Get-Process | Where-Object { $_.MainWindowTitle } |
            ForEach-Object { "{0} [{1}]: {2}" -f $_.ProcessName, $_.Id, $_.MainWindowTitle } |
            Set-Content -LiteralPath (Join-Path $Results "$Name.windows.txt") -Encoding UTF8
    } catch { }
}

function Read-Shared([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite, Delete')
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::Default)
        $t = $sr.ReadToEnd(); $sr.Close()
        return $t
    } catch { return "" }
}

function Get-ShortHash([string]$s) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $h = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    return (($h[0..3] | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Get-EnvSummary {
    # Key names, whether each is set, and an 8-hex hash of the value -- enough to compare
    # secrets across runs without writing them out.
    $p = Join-Path $App ".env"
    if (-not (Test-Path -LiteralPath $p)) { return [ordered]@{ exists = $false } }
    $bytes = [System.IO.File]::ReadAllBytes($p)
    $lines = [System.IO.File]::ReadAllLines($p)
    $keys = @(); $dups = @(); $seenK = @{}
    foreach ($l in $lines) {
        if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $k = $matches[1]; $v = $matches[2].Trim()
            if ($seenK.ContainsKey($k)) { $dups += $k }
            $seenK[$k] = $true
            if ($v) { $keys += ("{0}=<set len={1} h={2}>" -f $k, $v.Length, (Get-ShortHash $v)) } else { $keys += ("{0}=<empty>" -f $k) }
        }
    }
    return [ordered]@{
        exists = $true; bytes = $bytes.Length; lines = $lines.Count
        bom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB)
        keys = $keys; duplicate_keys = $dups
    }
}

function Get-StateJson {
    $p = Join-Path $App ".setup\state.json"
    if (Test-Path -LiteralPath $p) { return (Get-Content -Raw -LiteralPath $p) }
    return $null
}

function Get-Descendants([int]$RootPid) {
    $all = @(Get-CimInstance Win32_Process)
    $out = @(); $frontier = @($RootPid)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($f in $frontier) {
            foreach ($c in $all) {
                if ($c.ParentProcessId -eq $f -and $c.ProcessId -ne $f) {
                    $out += ("{0} pid={1} ppid={2} :: {3}" -f $c.Name, $c.ProcessId, $c.ParentProcessId, $c.CommandLine)
                    $next += [int]$c.ProcessId
                }
            }
        }
        $frontier = $next
    }
    return $out
}

function Get-AppProcesses {
    $esc = [regex]::Escape($App)
    return @(Get-CimInstance Win32_Process | Where-Object {
        ($_.CommandLine -and $_.CommandLine -match $esc) -or ($_.ExecutablePath -and $_.ExecutablePath -match $esc)
    } | ForEach-Object { "{0} pid={1} ppid={2} :: {3}" -f $_.Name, $_.ProcessId, $_.ParentProcessId, $_.CommandLine })
}

function Copy-SetupLogs([string]$Tag) {
    $src = Join-Path $App ".setup\logs"
    if (Test-Path -LiteralPath $src) {
        $dst = Join-Path $Results "$Tag.setup_logs"
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Lines quickstart and its children print, in the order a person meets them. First sighting of
# each is timed from the start of the run.
$Markers = @(
    @("lock_ok", "SAFE TO RE-RUN"),
    @("step1", "STEP 1/7"),
    @("ca_bundle", "Using this machine's trusted roots"),
    @("proxy", "Using this PC's proxy"),
    @("uv_download", "Attempting a no-admin install of 'uv'"),
    @("uv_sig", "uv.exe Authenticode signature"),
    @("uv_provision", "Using uv at"),
    @("python_found", "Using Python interpreter"),
    @("resuming", "RESUMING:"),
    @("b_venv", "==> Ensuring virtual environment"),
    @("b_deps", "==> Installing Python dependencies"),
    @("b_env", "==> Preparing .env"),
    @("b_edge", "==> Checking Microsoft Edge"),
    @("b_devtunnel", "==> Checking Dev Tunnels CLI"),
    @("b_connector", "==> Generating Copilot Studio connector"),
    @("b_verify", "==> Verifying environment"),
    @("b_complete", "All steps complete"),
    @("action_needed", "ACTION NEEDED"),
    @("b_failed", "FAILED at step"),
    @("boot_stopped", "Bootstrap stopped with exit code"),
    @("step2", "STEP 2/7"),
    @("step3", "STEP 3/7"),
    @("access_q", "How should Copilot Studio be allowed"),
    @("access_none", "Recorded: no access grant"),
    @("step4", "STEP 4/7"),
    @("dt_install", "[1/4]"),
    @("dt_signed", "[2/4] sign-in: already"),
    @("dt_signin", "[2/4] sign-in required"),
    @("dt_wait", "Waiting for you to finish the Microsoft sign-in"),
    @("dt_devcode", "DEVICE CODE"),
    @("dt_devicelogin", "devicelogin"),
    @("dt_notsigned", "still not signed in"),
    @("dt_fail", "Dev Tunnel setup did not finish"),
    @("dt_url_missing", "Dev Tunnel URL is not ready"),
    @("convenience", "Convenience setup"),
    @("core_start", "Starting the MCP server"),
    @("step5", "STEP 5/7"),
    @("step6", "STEP 6/7"),
    @("step7", "STEP 7/7"),
    @("m365_signin", "Sign in to M365 on the companion browser"),
    @("health", "Health check"),
    @("setup_complete", "SETUP COMPLETE"),
    @("setup_incomplete", "SETUP INCOMPLETE"),
    @("setup_notconfirmed", "SETUP NOT CONFIRMED")
)
$ShotMarkers = @("dt_signin", "dt_devcode", "dt_fail", "boot_stopped", "action_needed")

# KEYS A PERSON TYPES, and when. quickstart's console keeps its real keyboard input (stdin is NOT
# redirected): each answer is written into that console's input buffer by typer.exe once the
# prompt is on screen. A redirected stdin is not what a person has -- at EOF a child can behave
# differently (measured: the first admin run ended with ^C / 0xC000013A 17 s into the device
# code) -- so it is not used. Rows: text that must be on screen, keys, seconds to wait first.
$TypedAnswers = @(
    @("Press A, T or N", "N", 2),
    # The window-holding `pause` after every stop: a person reads, then presses a key.
    @("Dev Tunnel setup did not finish", "ENTER", 8),
    @("Dev Tunnel URL is not ready", "ENTER", 8),
    @("Bootstrap stopped with exit code", "ENTER", 8),
    @("Refusing to continue", "ENTER", 8),
    @("SETUP COMPLETE", "ENTER", 8),
    @("SETUP INCOMPLETE", "ENTER", 8),
    @("SETUP NOT CONFIRMED", "ENTER", 8)
)

function Send-Keys([int]$ConsolePid, [string]$Keys) {
    $typer = Join-Path $Harness "typer.exe"
    try {
        $t = Start-Process -FilePath $typer -ArgumentList @("$ConsolePid", $Keys) -WindowStyle Hidden -Wait -PassThru
        return $t.ExitCode
    } catch { return "error: $_" }
}

function Invoke-Quickstart {
    param([string]$Tag, [string]$KillAfterMarker = "", [double]$KillDelaySec = 0, [int]$CapSec = 1800)
    Log "=== ${Tag}: start (kill after '$KillAfterMarker' +${KillDelaySec}s, cap ${CapSec}s)"
    $log = Join-Path $Results "$Tag.transcript.txt"
    $qs = Join-Path $App "quickstart.bat"
    # Output goes to the transcript (what the window would have shown); input stays the console.
    $cmdArgs = '/d /s /c ""' + $qs + '" > "' + $log + '" 2>&1"'
    $before = [ordered]@{ state_json = (Get-StateJson); env = (Get-EnvSummary); venv_python = (Test-Path (Join-Path $App ".venv\Scripts\python.exe")) }
    $start = Get-Date
    $p = Start-Process -FilePath "cmd.exe" -ArgumentList $cmdArgs -WorkingDirectory $App -PassThru
    $null = $p.Handle
    $seen = [ordered]@{}
    $shots = @{}
    $typed = @(); $typeDue = @{}
    $killDeadline = $null; $killReason = $null; $atKill = $null
    $fast = ($KillAfterMarker -and $KillDelaySec -eq 0)
    while ($true) {
        $exited = $p.HasExited
        $text = Read-Shared $log
        foreach ($m in $Markers) {
            if (-not $seen.Contains($m[0]) -and $text.Contains($m[1])) {
                $seen[$m[0]] = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
                Log ("{0}: marker {1} at {2}s" -f $Tag, $m[0], $seen[$m[0]])
                if ($ShotMarkers -contains $m[0]) { $shots[$m[0]] = (Get-Date).AddSeconds(4) }
            }
        }
        foreach ($k in @($shots.Keys)) {
            if ($shots[$k] -and (Get-Date) -ge $shots[$k]) { Save-Screen "$Tag.$k"; $shots[$k] = $null }
        }
        if ($exited) { break }
        foreach ($ta in $TypedAnswers) {
            $key = $ta[0]
            if (-not $typeDue.ContainsKey($key) -and $text.Contains($ta[0])) { $typeDue[$key] = (Get-Date).AddSeconds([double]$ta[2]) }
            if ($typeDue[$key] -and (Get-Date) -ge $typeDue[$key]) {
                $trc = Send-Keys $p.Id $ta[1]
                $at = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
                $typed += ("{0}s typed '{1}' at prompt '{2}' (typer rc={3})" -f $at, $ta[1], $ta[0], $trc)
                Log ("{0}: typed '{1}' for '{2}' rc={3}" -f $Tag, $ta[1], $ta[0], $trc)
                $typeDue[$key] = $null
            }
        }
        if ($KillAfterMarker -and -not $killDeadline -and $seen.Contains($KillAfterMarker)) {
            $killDeadline = (Get-Date).AddSeconds($KillDelaySec)
        }
        if ($killDeadline -and (Get-Date) -ge $killDeadline) { $killReason = "planned: ${KillDelaySec}s after marker '$KillAfterMarker'"; break }
        if (((Get-Date) - $start).TotalSeconds -gt $CapSec) { $killReason = "cap ${CapSec}s reached (still running)"; break }
        if ($fast) { Start-Sleep -Milliseconds 10 } else { Start-Sleep -Milliseconds 250 }
    }
    $rc = $null; $survivors = @()
    if ($killReason) {
        $atKill = [ordered]@{
            t = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
            process_tree = @(Get-Descendants $p.Id)
            env = (Get-EnvSummary)
            env_tmp_files = @(Get-ChildItem -LiteralPath $App -Force -Filter ".env*" -ErrorAction SilentlyContinue | ForEach-Object { "{0} {1}" -f $_.Name, $_.Length })
            state_json = (Get-StateJson)
        }
        Save-Screen "$Tag.at_kill"
        Log "${Tag}: killing tree of cmd pid $($p.Id): $killReason"
        & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null
        Start-Sleep -Seconds 3
        $survivors = @(Get-AppProcesses)
        if ($survivors.Count -gt 0) { Log "${Tag}: survivors after kill: $($survivors.Count)" }
    } else {
        $rc = $p.ExitCode
    }
    $elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
    Save-Screen "$Tag.end"
    $vpy = Join-Path $App ".venv\Scripts\python.exe"
    $text = Read-Shared $log
    # What STEP 2 showed vs what .env holds (hash only).
    $shown = [ordered]@{}
    foreach ($ln in ($text -split "`r?`n")) {
        if ($ln -match 'Bearer token\s+\(MCP_API_KEY\)\s*:\s*(\S+)') { $shown["MCP_API_KEY"] = Get-ShortHash $matches[1].Trim() }
        if ($ln -match 'Unlock password \(MCP_UNLOCK_PASSWORD\)\s*:\s*(\S+)') { $shown["MCP_UNLOCK_PASSWORD"] = Get-ShortHash $matches[1].Trim() }
    }
    $run = [ordered]@{
        tag = $Tag; typed = $typed; kill_rule = "$KillAfterMarker +$KillDelaySec s"
        elapsed_sec = $elapsed; exit_code = $rc; killed = [bool]$killReason; kill_reason = $killReason
        markers_sec = $seen; before = $before; at_kill = $atKill; survivors_after_kill = $survivors
        after = [ordered]@{ state_json = (Get-StateJson); env = (Get-EnvSummary); venv_python = (Test-Path $vpy); step2_shown_hash = $shown }
        transcript_tail = @(($text -split "`r?`n") | Select-Object -Last 40)
    }
    Copy-SetupLogs $Tag
    Log "=== ${Tag}: end rc=$rc killed=$([bool]$killReason) elapsed=${elapsed}s"
    return $run
}

function Get-StartAllProcs {
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and ($_.CommandLine -match 'start_all\.ps1' -or $_.CommandLine -match 'start_all_hidden\.vbs' -or $_.CommandLine -match 'start_all\.bat')
    })
}

function Wait-StartAllIdle([string]$Tag, [int]$CapSec) {
    $start = Get-Date; $quiet = 0; $everSeen = $false; $peak = 0; $shotAt = $start.AddSeconds(45); $shot = $false
    while ($true) {
        $n = @(Get-StartAllProcs).Count
        if ($n -gt 0) { $everSeen = $true }
        if ($n -gt $peak) { $peak = $n }
        if (-not $shot -and (Get-Date) -ge $shotAt) { Save-Screen "$Tag.t45"; $shot = $true }
        $el = ((Get-Date) - $start).TotalSeconds
        if ($n -eq 0 -and ($everSeen -or $el -gt 20)) { $quiet++ } else { $quiet = 0 }
        if ($quiet -ge 3) { return [ordered]@{ idle_after_sec = [math]::Round($el, 1); timed_out = $false; peak_processes = $peak } }
        if ($el -gt $CapSec) {
            Save-Screen "$Tag.cap"
            $left = @(Get-StartAllProcs | ForEach-Object { "{0} pid={1} :: {2}" -f $_.Name, $_.ProcessId, $_.CommandLine })
            return [ordered]@{ idle_after_sec = $null; timed_out = $true; peak_processes = $peak; still_running = $left }
        }
        Start-Sleep -Seconds 1
    }
}

function Get-StackSnapshot {
    $health = $null
    try { $health = (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5 -UseBasicParsing).StatusCode } catch { $health = "error: " + $_.Exception.Message }
    $ports = @()
    foreach ($port in 8000, 8765, 9222, 9223) {
        $c = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        $ports += ("{0}={1}" -f $port, $(if ($c.Count) { "listen pid " + $c[0].OwningProcess } else { "closed" }))
    }
    $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|powershell|msedge|devtunnel|wscript)\.exe$' } |
        Group-Object Name | ForEach-Object { "{0} x{1}" -f $_.Name, $_.Count })
    return [ordered]@{ health = $health; ports = $ports; process_counts = $procs }
}

function Invoke-StartAllClicks([string]$Tag, [int]$Clicks) {
    Log "=== ${Tag}: $Clicks click(s) of start_all.bat"
    $bat = Join-Path $App "start_all.bat"
    $runsPath = Join-Path $App ".setup\logs\start_all_runs.jsonl"
    $linesBefore = 0
    if (Test-Path -LiteralPath $runsPath) { $linesBefore = @(Get-Content -LiteralPath $runsPath).Count }
    $start = Get-Date
    $cmds = @()
    for ($i = 1; $i -le $Clicks; $i++) {
        $clog = Join-Path $Results ("{0}.click{1}.txt" -f $Tag, $i)
        $a = '/d /s /c ""' + $bat + '" > "' + $clog + '" 2>&1"'
        $cmds += Start-Process -FilePath "cmd.exe" -ArgumentList $a -WorkingDirectory $App -WindowStyle Minimized -PassThru
    }
    $launchSpread = [math]::Round(((Get-Date) - $start).TotalMilliseconds)
    $wait = Wait-StartAllIdle $Tag $StartAllCapSec
    $batRcs = @($cmds | ForEach-Object { if ($_.HasExited) { $_.ExitCode } else { "running" } })
    $newLines = @()
    if (Test-Path -LiteralPath $runsPath) { $newLines = @(Get-Content -LiteralPath $runsPath | Select-Object -Skip $linesBefore) }
    $sumPath = Join-Path $App ".setup\logs\start_all_summary.txt"
    $sum = $null; if (Test-Path -LiteralPath $sumPath) { $sum = Get-Content -Raw -LiteralPath $sumPath }
    Save-Screen "$Tag.end"
    $r = [ordered]@{
        tag = $Tag; clicks = $Clicks; launch_spread_ms = $launchSpread
        elapsed_sec = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
        wait = $wait; bat_exit_codes = $batRcs
        start_all_runs_new_lines = $newLines; start_all_summary = $sum
        stack = (Get-StackSnapshot)
    }
    Copy-SetupLogs $Tag
    Log "=== ${Tag}: end idle_after=$($wait.idle_after_sec) timed_out=$($wait.timed_out) new_run_lines=$($newLines.Count)"
    return $r
}

function Get-EnvironmentProbe {
    $sys = [ordered]@{}
    try { $os = Get-CimInstance Win32_OperatingSystem; $sys.os = "{0} {1} build {2}" -f $os.Caption, $os.OSArchitecture, $os.BuildNumber } catch { }
    $sys.ps_version = $PSVersionTable.PSVersion.ToString()
    $sys.user = "$env:USERDOMAIN\$env:USERNAME"
    $sys.culture = (Get-Culture).Name
    $sys.oem_codepage = (& cmd.exe /c chcp) -join " "
    foreach ($exe in "python", "py", "git", "winget", "wscript", "choice", "uv", "devtunnel") {
        $c = Get-Command $exe -ErrorAction SilentlyContinue | Select-Object -First 1
        $sys["where_$exe"] = $(if ($c) { $c.Source } else { "(not found)" })
    }
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ }
    $sys.edge = $(if ($edge) { $edge -join "; " } else { "(not found)" })
    $net = [ordered]@{}
    $net.ipconfig = (& ipconfig.exe /all | Out-String)
    $net.default_routes = (& route.exe print -4 0.0.0.0 | Out-String)
    $net.winhttp_proxy = (& netsh.exe winhttp show proxy | Out-String)
    try { $net.wininet = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' | Select-Object ProxyEnable, ProxyServer, AutoConfigURL | Out-String) } catch { }
    $net.env_proxy = "HTTPS_PROXY=$env:HTTPS_PROXY HTTP_PROXY=$env:HTTP_PROXY"
    $tc = New-Object System.Net.Sockets.TcpClient
    try { $ok = $tc.ConnectAsync("127.0.0.1", 3128).Wait(2000); $net.loopback_3128 = $(if ($ok -and $tc.Connected) { "open" } else { "closed/timeout" }) } catch { $net.loopback_3128 = "closed: " + $_.Exception.InnerException.Message } finally { $tc.Close() }
    $probes = @()
    foreach ($u in "https://astral.sh/uv/install.ps1", "https://github.com/", "https://pypi.org/simple/pip/", "https://files.pythonhosted.org/", "https://aka.ms/TunnelsCliDownload/win-x64", "https://global.rel.tunnels.api.visualstudio.com/", "https://login.microsoftonline.com/", "https://copilotstudio.microsoft.com/") {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 5
            $probes += ("{0} -> {1} in {2}ms" -f $u, $r.StatusCode, $sw.ElapsedMilliseconds)
        } catch {
            $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch { }
            $probes += ("{0} -> {1} in {2}ms ({3})" -f $u, $(if ($code) { "HTTP $code" } else { "FAILED" }), $sw.ElapsedMilliseconds, $_.Exception.Message)
        }
    }
    $net.probes = $probes
    return [ordered]@{ system = $sys; network = $net }
}

function Get-Identity {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return [ordered]@{
        user = $id.Name
        is_admin_role = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        whoami_groups = (& whoami.exe /groups /fo list | Out-String)
        whoami_priv = (& whoami.exe /priv | Out-String)
        userprofile = $env:USERPROFILE; localappdata = $env:LOCALAPPDATA
    }
}

# ===========================================================================================
# ADMIN PHASE (the sandbox's own logon, WDAGUtilityAccount, an administrator). It only prepares:
# a local STANDARD user (not in Administrators), a readable copy of the tree, a writable output
# folder, the console typer -- then starts the user phase AS THAT USER on this desktop and relays
# its output to the mapped results folder. It runs nothing from the product tree.
# ===========================================================================================
$LogonSrc = @"
using System;
using System.Runtime.InteropServices;
public static class SbLogon {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO { public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError; }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcessWithLogonW(string user, string domain, string pw, uint logonFlags, string app,
        string cmd, uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    // LOGON_WITH_PROFILE, the user's OWN environment (env = NULL), this desktop -- what runas does.
    public static int Start(string user, string pw, string cmd, string cwd) {
        STARTUPINFO si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si);
        si.dwFlags = 1; si.wShowWindow = 7; // STARTF_USESHOWWINDOW, SW_SHOWMINNOACTIVE
        PROCESS_INFORMATION pi;
        if (!CreateProcessWithLogonW(user, ".", pw, 1, null, cmd, 0x10, IntPtr.Zero, cwd, ref si, out pi))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return pi.dwProcessId;
    }
}
"@

# typer.exe <console-owner-pid> <keys>: writes key events into that console's input buffer, the
# same events a keyboard produces. "ENTER" means the Enter key.
$TyperSrc = @"
using System;
using System.Runtime.InteropServices;
public static class SbTyper {
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool AttachConsole(uint pid);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string n, uint a, uint s, IntPtr sa, uint c, uint f, IntPtr t);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool WriteConsoleInputW(IntPtr h, INPUT_RECORD[] b, uint n, out uint w);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Explicit)]
    struct INPUT_RECORD { [FieldOffset(0)] public ushort EventType; [FieldOffset(4)] public KEY_EVENT_RECORD KeyEvent; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct KEY_EVENT_RECORD { public int bKeyDown; public ushort wRepeatCount; public ushort wVirtualKeyCode;
        public ushort wVirtualScanCode; public char UnicodeChar; public uint dwControlKeyState; }
    public static int Main(string[] args) {
        uint pid = uint.Parse(args[0]);
        FreeConsole();
        if (!AttachConsole(pid)) return 2;
        IntPtr h = CreateFileW("CONIN$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
        if (h == new IntPtr(-1)) return 3;
        for (int i = 1; i < args.Length; i++) {
            string s = args[i] == "ENTER" ? "\r" : args[i];
            foreach (char ch in s) {
                ushort vk = ch == '\r' ? (ushort)0x0D : (ushort)Char.ToUpperInvariant(ch);
                INPUT_RECORD[] recs = new INPUT_RECORD[2];
                for (int k = 0; k < 2; k++) {
                    recs[k].EventType = 1; recs[k].KeyEvent.bKeyDown = (k == 0) ? 1 : 0;
                    recs[k].KeyEvent.wRepeatCount = 1; recs[k].KeyEvent.wVirtualKeyCode = vk; recs[k].KeyEvent.UnicodeChar = ch;
                }
                uint w; if (!WriteConsoleInputW(h, recs, 2, out w)) return 4;
            }
        }
        CloseHandle(h); FreeConsole(); return 0;
    }
}
"@

function Invoke-AdminPhase {
    $Summary.phase = "admin"
    $Summary.admin_identity = [ordered]@{ user = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
    # The tree comes from `git archive HEAD` on the host. Refuse anything that looks like local
    # state or a secret, so a mistake on the host side cannot leak into (or skew) this run.
    $src = Join-Path $Harness "src"
    $forbidden = @(".env", ".setup", ".fleet", ".venv") | Where-Object { Test-Path -LiteralPath (Join-Path $src $_) }
    $Summary.source_forbidden_present = @($forbidden)
    if ($forbidden.Count -gt 0) { throw "source tree carries local state: $($forbidden -join ', ')" }

    $hs = "C:\hsrc"; $ho = "C:\hout"
    foreach ($d in $hs, $ho) { if (Test-Path $d) { Remove-Item $d -Recurse -Force }; New-Item -ItemType Directory -Force -Path $d | Out-Null }
    Copy-Item -LiteralPath $src -Destination (Join-Path $hs "src") -Recurse -Force
    Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $hs "sandbox_driver.ps1") -Force
    Add-Type -TypeDefinition $TyperSrc -OutputAssembly (Join-Path $hs "typer.exe") -OutputType ConsoleApplication
    Log "prepared $hs (tree, driver, typer.exe)"

    # A standard local user: Users only, never Administrators. The password lives only in this
    # disposable sandbox's memory.
    $u = "sbuser"
    $chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $bytes = New-Object byte[] 20; $rng.GetBytes($bytes)
    $pw = "Sb9!" + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] }))
    $out = (& net.exe user $u $pw /add /y 2>&1 | Out-String)
    Log "net user add: $($out.Trim())"
    $admins = (& net.exe localgroup Administrators 2>&1 | Out-String)
    $Summary.user_in_administrators = ($admins -match "(?m)^\s*$u\s*$")
    if ($Summary.user_in_administrators) { throw "$u is in Administrators; refusing to run as an admin" }
    & icacls.exe $hs /grant "${u}:(OI)(CI)RX" /T /Q | Out-Null
    & icacls.exe $ho /grant "${u}:(OI)(CI)M" /T /Q | Out-Null

    Add-Type -TypeDefinition $LogonSrc
    $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $hs "sandbox_driver.ps1") +
        '" -Phase user -Scenario ' + $Scenario + ' -Harness "' + $hs + '" -Results "' + $ho + '" -QuickstartCapSec ' +
        $QuickstartCapSec + ' -StartAllCapSec ' + $StartAllCapSec
    $upid = [SbLogon]::Start($u, $pw, $cmd, $hs)
    $pw = $null
    Log "user phase started as .\$u, pid $upid"
    $Summary.user_phase_pid = $upid
    Save-Summary

    # Relay the user's output to the mapped results folder until it says it is done.
    $deadline = (Get-Date).AddMinutes(150)
    while ((Get-Date) -lt $deadline) {
        & robocopy.exe $ho $Results /E /R:0 /W:0 /NJH /NJS /NFL /NDL /NP | Out-Null
        if (Test-Path (Join-Path $ho "USER_DONE.json")) { break }
        if (-not (Get-Process -Id $upid -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $ho "USER_DONE.json"))) {
            Start-Sleep -Seconds 5
            if (-not (Test-Path (Join-Path $ho "USER_DONE.json"))) { Log "user phase pid $upid exited without USER_DONE.json"; break }
        }
        Start-Sleep -Seconds 10
    }
    & robocopy.exe $ho $Results /E /R:0 /W:0 /NJH /NJS /NFL /NDL /NP | Out-Null
}

# ===========================================================================================
# USER PHASE (the standard user). Everything a person does, in that person's own profile.
# ===========================================================================================
function Invoke-UserPhase {
    $Summary.phase = "user"
    $Summary.identity = Get-Identity
    Log ("identity: {0} admin_role={1}" -f $Summary.identity.user, $Summary.identity.is_admin_role)
    if ($Summary.identity.is_admin_role) { throw "user phase is running with the Administrators role; refusing" }
    $src = Join-Path $Harness "src"
    if (Test-Path -LiteralPath $App) { Remove-Item -LiteralPath $App -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    Copy-Item -Path (Join-Path $src "*") -Destination $App -Recurse -Force
    Log "copied source tree to $App"
    $Summary.environment = Get-EnvironmentProbe
    Save-Summary
    Save-Screen "00.desktop"

    if ($Scenario -eq "AC") {
        $a = Invoke-Quickstart -Tag "A_quickstart" -CapSec $QuickstartCapSec
        $Summary.runs += $a; Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C1_start_all" 1); Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C2_start_all_again" 1); Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C3_ten_clicks" 10); Save-Summary
        # doctor is NOT launched: only quickstart.bat and start_all.bat may be (owner rule). A
        # doctor summary is read only if quickstart itself produced one.
        $dsum = $null; $dp = Join-Path $App ".setup\logs\doctor_summary.txt"; if (Test-Path $dp) { $dsum = Get-Content -Raw $dp }
        $Summary.runs += [ordered]@{ tag = "C_final_observation"; doctor_summary_from_quickstart = $dsum; stack = (Get-StackSnapshot) }
        Copy-SetupLogs "C_final"
        Save-Screen "C_final"
    } elseif ($Scenario -eq "C") {
        # C on its own: the install is brought to where a person must sign in to devtunnel, and
        # the window is closed there (a fresh sandbox has no install; this is the most of one a
        # person can make without a devtunnel account). Then the daily start.
        $Summary.runs += (Invoke-Quickstart -Tag "C0_quickstart_until_signin" -KillAfterMarker "dt_signin" -KillDelaySec 8 -CapSec $QuickstartCapSec); Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C1_start_all" 1); Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C2_start_all_again" 1); Save-Summary
        $Summary.runs += (Invoke-StartAllClicks "C3_ten_clicks" 10); Save-Summary
        $Summary.runs += [ordered]@{ tag = "C_final_observation"; stack = (Get-StackSnapshot) }
        Copy-SetupLogs "C_final"
        Save-Screen "C_final"
    } elseif ($Scenario -eq "B") {
        $Summary.runs += (Invoke-Quickstart -Tag "B1_kill_in_deps" -KillAfterMarker "b_deps" -KillDelaySec 25 -CapSec $QuickstartCapSec); Save-Summary
        $Summary.runs += (Invoke-Quickstart -Tag "B2_kill_in_env" -KillAfterMarker "b_env" -KillDelaySec 0 -CapSec $QuickstartCapSec); Save-Summary
        $Summary.runs += (Invoke-Quickstart -Tag "B3_resume" -KillAfterMarker "dt_signin" -KillDelaySec 8 -CapSec $QuickstartCapSec); Save-Summary
    } else {
        throw "unknown scenario '$Scenario'"
    }
}

try {
    Log "driver start: phase=$Phase scenario=$Scenario app=$App"
    if ($Phase -eq "user") { Invoke-UserPhase } else { Invoke-AdminPhase }
} catch {
    $Summary.driver_error = "$_"
    Log "DRIVER ERROR ($Phase): $_"
} finally {
    $Summary.finished = (Get-Date).ToString("o")
    $Summary.total_sec = [math]::Round(((Get-Date) - $T0).TotalSeconds, 1)
    if ($Phase -eq "user") {
        Save-Summary
        [ordered]@{ finished = $Summary.finished; total_sec = $Summary.total_sec; error = $Summary.driver_error } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Results "USER_DONE.json") -Encoding UTF8
    } else {
        Save-Summary
        [ordered]@{ finished = $Summary.finished; total_sec = $Summary.total_sec; error = $Summary.driver_error } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Results "DONE.json") -Encoding UTF8
    }
    Log "driver done ($Phase)"
}
