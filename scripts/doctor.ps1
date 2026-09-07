# =============================================================================
#  doctor.ps1 -- one-glance health check for the m365-copilot-companion-mcp stack.
#  Verifies every link (secrets -> server -> tunnel -> Edge -> M365 sign-in ->
#  auth) and prints a GREEN/RED checklist with the exact fix for each RED line,
#  so "is it actually working?" is answered in one run. Read-only; safe any time.
#  ASCII / ENGLISH ONLY (cmd/console safe).
#
#  -Json: emit ONE compressed JSON array line on stdout (id/ok/name/fix/optional/
#  info/skipped/indeterminate per check) instead of the colored checklist. This is the single
#  machine-readable source of truth that scripts\repair.ps1 (and any other tool,
#  e.g. a cockpit UI) parses to decide what to fix -- doctor stays READ-ONLY
#  (detect only); it never repairs anything itself.
# =============================================================================
param(
    [switch]$Json
)
$ErrorActionPreference = "SilentlyContinue"

# In -Json mode, stdout must carry ONLY the one JSON line (so callers can parse it
# cleanly). Rather than gate every single Write-Host call below, shadow the Write-Host
# cmdlet with a no-op function for the rest of this script run -- a locally defined
# function always wins command resolution over a cmdlet of the same name, so every
# existing Write-Host call site below is silenced automatically and needs no edits.
if ($Json) {
    function Write-Host {
        param(
            [Parameter(Position = 0, ValueFromPipeline = $true)] $Object,
            [ConsoleColor]$ForegroundColor,
            [ConsoleColor]$BackgroundColor,
            [switch]$NoNewline,
            $Separator
        )
        # suppressed in -Json mode -- stdout carries only the final JSON line
    }
}
# This script lives in <repo>\scripts; the .env it reads is at the REPO ROOT (one level up).
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir

# Shared PURE helpers (Get-SupervisorArgTunnel / Get-BareTunnelName /
# Test-SupervisorTunnelDrift) -- see tunnel_name_util.ps1's header comment. No
# top-level side effects, so dot-sourcing it here is safe.
. (Join-Path $scriptDir "tunnel_name_util.ps1")

# --- load .env into a hashtable -------------------------------------------------
$envv = @{}
$envPath = Join-Path $repo ".env"
if (Test-Path $envPath) {
    foreach ($ln in Get-Content $envPath) {
        if ($ln -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envv[$matches[1]] = $matches[2].Trim() }
    }
}

$script:ok = 0; $script:bad = 0; $script:warn = 0
# Machine-readable accumulator: one entry per check, in the exact order checks run
# (== the dependency order documented at the top of this file). This -- not the
# colored console text -- is the single source of truth a repair dispatcher reads.
$script:results = @()
function Add-Result([string]$id, [bool]$pass, [string]$name, [string]$fix, [bool]$optional, [bool]$info, [bool]$skipped, [bool]$indeterminate = $false) {
    $script:results += [PSCustomObject]@{
        id       = $id
        ok       = $pass
        name     = $name
        fix      = $fix
        optional = $optional
        info     = $info
        skipped  = $skipped
        indeterminate = $indeterminate
    }
}
function Check-TriState([string]$id, [string]$name, [scriptblock]$test, [string]$fix) {
    # `$null` means the dependency could not be queried reliably (for example, the devtunnel CLI
    # timed out during a network switch). It is neither a security PASS nor a repairable FAIL.
    $value = $null
    try { $value = & $test } catch { $value = $null }
    if ($null -eq $value) {
        Add-Result $id $false $name $fix $false $false $false $true
        Write-Host ("  [WARN] " + $name + " (temporarily indeterminate)") -ForegroundColor Yellow
        Write-Host ("         retry: " + $fix) -ForegroundColor DarkYellow
        $script:warn++
        return
    }
    $pass = [bool]$value
    Add-Result $id $pass $name $fix $false $false $false $false
    if ($pass) {
        Write-Host ("  [ OK ] " + $name) -ForegroundColor Green
        $script:ok++
    } else {
        Write-Host ("  [FAIL] " + $name) -ForegroundColor Red
        Write-Host ("         fix: " + $fix) -ForegroundColor Yellow
        $script:bad++
    }
}
function Check([string]$id, [string]$name, [scriptblock]$test, [string]$fix, [switch]$Optional, [switch]$Info) {
    $pass = $false
    try { $pass = [bool](& $test) } catch { $pass = $false }
    Add-Result $id $pass $name $fix $Optional.IsPresent $Info.IsPresent $false
    if ($pass) {
        Write-Host ("  [ OK ] " + $name) -ForegroundColor Green
        $script:ok++
    } elseif ($Info.IsPresent) {
        # Informational only: neither a pass nor a problem. It was counted as a failure.
        Write-Host ("  [INFO] " + $name) -ForegroundColor DarkGray
    } elseif ($Optional.IsPresent) {
        # OPTIONAL MEANS OPTIONAL. This counted into $script:bad exactly like a required check,
        # so the exit code -- which is the number of failures -- was inflated by things the
        # caller was explicitly told not to require, and the summary went red for them. A code
        # that cannot tell "a required thing is broken" from "an optional thing is absent"
        # cannot be acted on, which is why nobody acted on it.
        Write-Host ("  [WARN] " + $name + " (optional)") -ForegroundColor Yellow
        Write-Host ("         fix: " + $fix) -ForegroundColor DarkYellow
        $script:warn++
    } else {
        Write-Host ("  [FAIL] " + $name) -ForegroundColor Red
        Write-Host ("         fix: " + $fix) -ForegroundColor Yellow
        $script:bad++
    }
}
function Get-Json($url) { Invoke-RestMethod -Uri $url -TimeoutSec 4 -UseBasicParsing }

Write-Host ""
Write-Host "m365-copilot-companion-mcp  --  setup doctor" -ForegroundColor Cyan
Write-Host "============================================="

# 1. secrets / .env
Check "env_api_key" ".env present with a Bearer token (MCP_API_KEY)" `
    { $envv.ContainsKey('MCP_API_KEY') -and $envv['MCP_API_KEY'] } `
    "run quickstart.bat -- it creates .env with a fresh Bearer + unlock password"

Check "agent_url" "Agent URL configured (Copilot Studio agent pasted)" `
    { ($envv['MCP_FLEET_AGENT_URL']) -or ($envv['MCP_IMPL_AGENT_URL']) } `
    "double-click configure_env.bat and paste the Copilot Studio agent URL (README STEP 4)"

# WHETHER A SUPERVISOR IS RUNNING WAS NEVER CHECKED. The only supervisor-related check was
# "hosts the tunnel named in .env", which returns true when there is no supervisor at all --
# nothing to mismatch, so nothing to report. The server_up advice below then asked the reader
# to read that green as "the stack HAS been started", which it does not mean. Both branches of
# the advice looked identical from the output, so neither could be acted on.
function Get-RunningSupervisorCommandLineDoctor {
    try {
        $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and ($_.CommandLine -match 'supervisor\.ps1') } |
             Select-Object -First 1
        if ($p) { return $p.CommandLine }
    } catch { }
    return ""
}
$script:supervisorCmdLine = Get-RunningSupervisorCommandLineDoctor
# NOT RUNNING HAS TWO CAUSES AND THEY NEED DIFFERENT ANSWERS. start_all.ps1 now captures the
# supervisor's own startup output, so "never started" and "started and died" are separable --
# and when it died, "double-click start_all.bat" is the operation that just failed.
$script:supErrLog = Join-Path $repo ".setup\logs\supervisor.err.log"
$script:supFix = "the stack has not been started on this machine: double-click start_all.bat"
if (-not $script:supervisorCmdLine) {
    try {
        if (Test-Path $script:supErrLog) {
            $supLines = @(Get-Content $script:supErrLog -Tail 10 -ErrorAction Stop |
                          Where-Object { $_.Trim() })
            if ($supLines.Count -gt 0) {
                $script:supFix = ("the supervisor was started and STOPPED. It said:" +
                                  [Environment]::NewLine + "           " +
                                  ($supLines -join ([Environment]::NewLine + "           ")))
            }
        }
    } catch { }
}
Check "supervisor_running" "Supervisor running (it is what relaunches the server)" `
    { [bool]$script:supervisorCmdLine } `
    $script:supFix

# SAY WHY. DO NOT POINT AT A FILE. The reason the server died is already on this machine, and
# doctor knows the path; telling every user to go and open it is not a diagnosis a product can
# ship. One week passed with this failure reported and the log never read once.
$script:serverErrLog = Join-Path $repo ".setup\logs\server.err.log"
$script:serverErrHistory = Join-Path $repo ".setup\logs\server.err.history.log"
# TEXT ON STDERR IS NOT A CAUSE OF DEATH. A healthy server writes deprecation warnings and
# uvicorn's own "Application startup complete" / "Uvicorn running on ..." to stderr, so the
# last non-empty lines of this file are, on a machine whose log survives a launch that WORKED,
# a description of SUCCESS. Presenting them as the reason it died is worse than printing
# nothing: a reader given a false cause stops looking for the real one.
#
# Those same lines are still worth reading, because they say something true: a log ending in
# "startup complete" is from a launch that came up, so it cannot explain a server that is down
# now -- which means the current crash is producing no output at all, and that is the finding.
$script:STARTED_MARKERS = @("Application startup complete", "Uvicorn running on")

function Get-ServerLastOutput {
    # Returns @{ lines; started; source } -- or $null when neither file has anything.
    # Newest first: the live log holds only the CURRENT launch (Start-Process truncates it on
    # every relaunch, roughly once a minute), so a crash that produces nothing leaves it empty.
    # The history file is where the supervisor preserves each launch before truncating.
    foreach ($f in @($script:serverErrLog, $script:serverErrHistory)) {
        try {
            if (Test-Path $f) {
                $lines = @(Get-Content $f -Tail 12 -ErrorAction Stop | Where-Object { $_.Trim() })
                if ($lines.Count -gt 0) {
                    $started = $false
                    foreach ($m in $script:STARTED_MARKERS) {
                        if ($lines -match [regex]::Escape($m)) { $started = $true }
                    }
                    return @{ lines = $lines; started = $started; source = $f }
                }
            }
        } catch { }
    }
    return $null
}
# THE INTERPRETER THE SUPERVISOR WOULD ACTUALLY USE. Resolved exactly as supervisor.ps1
# resolves it, or this check answers a different question from the one that matters:
#     $Py = Join-Path $Root ".venv\Scripts\python.exe"
#     if (-not (Test-Path $Py)) { $Py = "python" }
# With no .venv that is bare `python`, which on a fresh Windows machine is usually the Store
# App Execution Alias -- a stub that opens the Microsoft Store and exits without writing a
# single byte to stderr. The server then dies silently, and a silent death is the one thing
# the crash log cannot explain.
$script:pyPath = Join-Path $repo ".venv\Scripts\python.exe"
$script:pyIsVenv = Test-Path $script:pyPath
if (-not $script:pyIsVenv) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    $script:pyPath = if ($cmd) { $cmd.Source } else { "" }
}
$script:pyProblem = ""
if (-not $script:pyPath) {
    $script:pyProblem = ("no Python at all: this checkout has no .venv and there is no python " +
                         "on PATH. Run setup.bat, which creates the .venv the supervisor looks " +
                         "for first.")
} elseif ($script:pyPath -match '\\WindowsApps\\') {
    # Named specifically because it is the failure that leaves NO trace: the alias exits
    # quietly, so there is nothing in the crash log to read and nothing to deduce.
    $script:pyProblem = ("this checkout has no .venv, so the supervisor falls back to bare " +
                         "'python' -- and on this machine that resolves to the Microsoft Store " +
                         "App Execution Alias (" + $script:pyPath + "). That stub opens the " +
                         "Store and exits without writing anything, so the server dies leaving " +
                         "no error to read. Run setup.bat to create the .venv.")
}
Check "python_runnable" "Python the supervisor would use (.venv, else PATH)" `
    { [bool]$script:pyPath -and -not $script:pyProblem } `
    $(if ($script:pyProblem) { $script:pyProblem } else { "run setup.bat to create the .venv" })

$script:serverFix = ""
if (-not $script:supervisorCmdLine) {
    $script:serverFix = ("the stack has not been started on this machine -- nothing is " +
                         "relaunching the server. Double-click start_all.bat.")
} else {
    $out = Get-ServerLastOutput
    if ($out -and -not $out.started) {
        $script:serverFix = ("the supervisor is relaunching the server and it is DYING ON " +
                             "STARTUP. Its last output was:" + [Environment]::NewLine +
                             "           " +
                             ($out.lines -join ([Environment]::NewLine + "           ")))
    } elseif ($out -and $out.started) {
        # SAY WHAT THIS LOG ACTUALLY SHOWS. It records a launch that reached "startup complete",
        # so it describes a server that came UP -- it cannot be the reason one is down now.
        $script:serverFix = ("the supervisor is relaunching the server and it is dying, but " +
                             "the only output on record is from a launch that STARTED " +
                             "SUCCESSFULLY, so it does not explain this failure. The current " +
                             "crash is producing no output at all. Do NOT run start_all.bat: " +
                             "the supervisor is already doing that on a loop.")
    } else {
        if ($script:pyProblem) {
            # The Python check above already found the cause; repeat it here rather than send
            # the reader hunting up the list for a red line they may not connect to this one.
            $script:serverFix = ("the supervisor is relaunching the server and it dies without " +
                                 "writing anything, because " + $script:pyProblem)
        } else {
            $script:serverFix = ("the supervisor is relaunching the server and it is dying on " +
                                 "startup, but it produced no output to explain why. The " +
                                 "interpreter check above passed, so this is not a missing " +
                                 "Python. Do NOT run start_all.bat: the supervisor is already " +
                                 "doing that on a loop.")
        }
    }
}

# 2. local MCP server
Check "server_up" "MCP server up (http://127.0.0.1:8000/health)" `
    { (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } `
    $script:serverFix

# 3. Dev Tunnel -- LAYERED diagnosis. A single reachability probe cannot tell "CLI not
#    installed" from "not logged in" from "tunnel deleted/expired" from "exists but not
#    currently hosted" -- and each of those needs a DIFFERENT fix; running start_all.bat
#    only ever fixes the last one. This mirrors supervisor.ps1's binary resolution
#    (prefer the winget-installed devtunnel.exe, else "devtunnel" on PATH) and its
#    login detection (devtunnel user show) so doctor agrees with what actually hosts
#    the tunnel. All calls are read-only (--version / user show / show) and bounded so
#    a hung/offline CLI cannot stall doctor. Chained: once the first link FAILs, the
#    remaining links are not meaningful, so they are shown as [SKIP] instead of being
#    run and possibly double-reporting -- only the one root cause counts toward $bad.
$turl = $envv['MCP_TUNNEL_URL']
$tname = $envv['MCP_TUNNEL_NAME']

$DevTunnel = "devtunnel"
$wingetDt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
# AND THE DIRECT DOWNLOAD. setup_devtunnel.ps1 falls back to
# %LOCALAPPDATA%\devtunnel\devtunnel.exe when winget is unavailable and appends that
# directory to the USER PATH -- which the already-running cmd session that launched this
# script cannot see. Looking only at the winget path and PATH means the CLI is installed
# and unfindable, on precisely the locked-down machines that needed the fallback.
if (-not (Test-Path $wingetDt)) {
    $directDt = Join-Path $env:LOCALAPPDATA "devtunnel\devtunnel.exe"
    if (Test-Path $directDt) { $wingetDt = $directDt }
}
if (Test-Path $wingetDt) { $DevTunnel = $wingetDt }

function Invoke-DevTunnelBounded([string[]]$dtArgs, [int]$timeoutSec) {
    # Start-Job + poll-with-deadline (same pattern as start_all.ps1's git-fetch timeout):
    # a hung or offline devtunnel CLI call times out instead of hanging doctor forever.
    try {
        $job = Start-Job -ScriptBlock {
            param($exe, $a)
            try { & $exe @a 2>&1 | Out-String } catch { "" }
        } -ArgumentList $DevTunnel, $dtArgs
    } catch { return $null }
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ($job.State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 150 }
    if ($job.State -eq 'Running') {
        try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
        try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
        return $null
    }
    $out = Receive-Job $job
    try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
    return $out
}

$script:tunnelChainBroken = $false
function TunnelCheck([string]$id, [string]$name, [scriptblock]$test, [string]$fix) {
    if ($script:tunnelChainBroken) {
        Write-Host ("  [SKIP] " + $name) -ForegroundColor DarkGray
        Write-Host ("         blocked by the Dev Tunnel check above -- fix that first, then re-run doctor") -ForegroundColor DarkGray
        Add-Result $id $false $name $fix $false $false $true
        return
    }
    $pass = $false
    try { $pass = [bool](& $test) } catch { $pass = $false }
    Add-Result $id $pass $name $fix $false $false $false
    if ($pass) {
        Write-Host ("  [ OK ] " + $name) -ForegroundColor Green
        $script:ok++
    } else {
        Write-Host ("  [FAIL] " + $name) -ForegroundColor Red
        Write-Host ("         fix: " + $fix) -ForegroundColor Yellow
        $script:bad++
        $script:tunnelChainBroken = $true
    }
}

TunnelCheck "tunnel_cli" "devtunnel CLI installed" `
    { $out = Invoke-DevTunnelBounded @('--version') 6; ($out) -and ($out -match 'Tunnel CLI version') } `
    "install it: winget install Microsoft.devtunnel   (then re-run start_all.bat)"

TunnelCheck "tunnel_login" "devtunnel signed in" `
    {
        $out = Invoke-DevTunnelBounded @('user', 'show') 6
        if (-not $out) { return $false }
        if ($out -match 'Not logged in' -or $out -match 'Login required') { return $false }
        if ($out -match 'Logged in') { return $true }
        return $false
    } `
    "run:  devtunnel login   (interactive, opens a browser -- this is the ONE step that needs a human; the supervisor will NOT host the tunnel until the CLI is logged in, so start_all.bat alone cannot fix this)"

TunnelCheck "tunnel_exists" "Dev Tunnel exists (MCP_TUNNEL_NAME)" `
    {
        if (-not $tname) { return $false }
        $out = Invoke-DevTunnelBounded @('show', $tname) 8
        ($out) -and ($out -match 'Tunnel ID') -and ($out -notmatch 'Tunnel not found')
    } `
    "the tunnel is missing or expired -- (re)create it: powershell -File scripts\setup_devtunnel.ps1"

# 3b. Privacy advisory -- independent of the tunnel dependency chain above (it reads only
# MCP_TUNNEL_NAME text, no devtunnel CLI call, so it is never [SKIP]'d by tunnelChainBroken).
# Detects whether the recorded tunnel name leaks an identifying (organization/user) token
# to the GLOBAL devtunnels.ms namespace. Mirrors Test-IdentifyingTunnelName in
# setup_devtunnel.ps1 and _is_identifying_tunnel_name in bootstrap.py -- keep all three
# in sync.
$TOKEN_SHA256 = "2a0341296bb96dc7d205036f9f693427809772f6136a46f58b04a1c492de9e04"  # gitleaks:allow
$FULLNAME_SHA256 = "5ba174b8e87faf4e8106e36a7cf5a901bbec3435d01fbd56914c2b0346858261"  # gitleaks:allow
function Get-Sha256HexDoctor([string]$s) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    } finally {
        $sha256.Dispose()
    }
    return (-join ($bytes | ForEach-Object { $_.ToString("x2") }))
}
function Test-IdentifyingTunnelName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $false }
    $lower = $name.ToLowerInvariant()
    if ((Get-Sha256HexDoctor $lower) -eq $FULLNAME_SHA256) { return $true }
    $tokens = @($lower -split '[^a-z0-9]+' | Where-Object { $_ })
    foreach ($t in $tokens) {
        if ((Get-Sha256HexDoctor $t) -eq $TOKEN_SHA256) { return $true }
    }
    $repoLeaf = (Split-Path -Leaf $repo).ToLowerInvariant()
    $userName = ("$env:USERNAME").ToLowerInvariant()
    foreach ($t in $tokens) {
        if (($repoLeaf -and $t -eq $repoLeaf) -or ($userName -and $t -eq $userName)) { return $true }
    }
    if (($repoLeaf -and $lower.Contains($repoLeaf)) -or ($userName -and $lower.Contains($userName))) { return $true }
    return $false
}

Check "tunnel_name_private" "Dev Tunnel name is private (no identifying token)" `
    { -not (Test-IdentifyingTunnelName $tname) } `
    "Tunnel name leaks an identifying token to the dev tunnel service. Recreate with a private name: powershell -File scripts\setup_devtunnel.ps1  (this changes the public URL -- re-paste MCP_TUNNEL_URL into Copilot Studio, then remove the old one: devtunnel delete <oldname>)."

# 3c. Ownership check -- catches an .env copied from another machine (MCP_TUNNEL_NAME
# names a tunnel THIS account does not own, so `devtunnel host` fails with a scopes
# error). Uses a tri-state check (not TunnelCheck) so it runs independently of the tunnel
# dependency short-circuit above -- an unowned name is informative even when e.g.
# the CLI is temporarily unreachable. A transient timeout is WARN/indeterminate rather than
# falsely declaring another account's tunnel or launching an unnecessary repair.
function Test-TunnelOwned([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $true }
    $bareName = (($name -split '\.')[0]).ToLowerInvariant()
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        # The CLI commonly needs 6-8s even on a healthy connection (token refresh/service round
        # trip), so each attempt gets 10s. Three bounded attempts still cap the worst case.
        $out = Invoke-DevTunnelBounded @('list') 10
        if ($out) {
            $ids = @($out -split "`r?`n" | ForEach-Object {
                if ($_ -match '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s') { $matches[1] }
            } | Where-Object { $_ } | ForEach-Object { (($_ -split '\.')[0]).ToLowerInvariant() })
            if ($ids.Count -gt 0) { return ($ids -contains $bareName) }
            # An explicit empty result is authoritative; arbitrary/partial output is not.
            if ($out -match '(?i)no (dev )?tunnels|0 tunnels') { return $false }
        }
        if ($attempt -lt 3) { Start-Sleep -Milliseconds 500 }
    }
    return $null
}

Check-TriState "tunnel_owned" "Dev Tunnel name is owned by this account (MCP_TUNNEL_NAME)" `
    { Test-TunnelOwned $tname } `
    "Re-run doctor.bat after connectivity settles. If it remains FAIL, run start_all.bat or: powershell -File scripts\heal_tunnel.ps1"

TunnelCheck "tunnel_serving" "Dev Tunnel host serving (public URL -> server)" `
    {
        if (-not $turl) { return $false }
        # MCP_TUNNEL_URL points at the /mcp path (e.g. https://host.devtunnels.ms/mcp);
        # /health is a SIBLING route at the tunnel origin, not nested under /mcp -- so
        # naively appending "/health" to $turl produced .../mcp/health, a 404 that made
        # this check FAIL even when the tunnel was being served correctly. Use the
        # origin (scheme+host) instead.
        $origin = ([Uri]$turl).GetLeftPart([UriPartial]::Authority)
        (Invoke-WebRequest -Uri ($origin + '/health') -TimeoutSec 7 -UseBasicParsing).StatusCode -eq 200
    } `
    "the tunnel exists but is not being served -- run start_all.bat (the supervisor hosts it). If this stays red while the checks above are green, MCP_TUNNEL_URL in .env may be stale -- compare it to the URL shown by 'devtunnel show <name>'."

# 3d. Supervisor/env match -- catches a RUNNING supervisor that is hosting a different
# (stale/borrowed) tunnel than .env currently names. This happens when .env was copied
# from another machine (naming a tunnel that machine's account owns), the supervisor
# started hosting that borrowed tunnel, and heal_tunnel.ps1's self-heal later repointed
# .env's MCP_TUNNEL_NAME to this account's own tunnel WHILE the already-running
# supervisor kept hosting the old one -- the exact scenario tunnel_serving above cannot
# distinguish from "not hosted at all". Uses Check (not TunnelCheck) so it runs
# independently of the tunnel dependency chain above: if no supervisor is running there
# is nothing to mismatch, so it passes.
# NOT APPLICABLE IS NOT A PASS. With no supervisor running there is nothing to compare .env
# against, and reporting that as green is what made "the stack was never started" look
# identical to "the stack is up and correct" -- the exact ambiguity the server advice depended
# on. The supervisor_running check above answers the prior question; this one only answers
# whether a RUNNING supervisor hosts the right tunnel.
$script:tunnelMatchFix = "The running supervisor is hosting a different (stale/borrowed) tunnel than .env names. Re-run start_all.bat -- it now stops the stale supervisor and re-hosts your own tunnel."
if (-not $script:supervisorCmdLine) {
    Write-Host ("  [SKIP] Running supervisor hosts the tunnel named in .env") -ForegroundColor DarkGray
    Write-Host ("         no supervisor is running -- see the supervisor check above") -ForegroundColor DarkGray
    Add-Result "tunnel_supervisor_match" $false "Running supervisor hosts the tunnel named in .env" $script:tunnelMatchFix $false $false $true
} else {
    Check "tunnel_supervisor_match" "Running supervisor hosts the tunnel named in .env" `
        { -not (Test-SupervisorTunnelDrift -RunningCommandLine $script:supervisorCmdLine -EnvTunnelName $tname) } `
        $script:tunnelMatchFix
}

# 4. Companion Edge (:9222) for the fleet/agent
Check "edge_companion" "Companion Edge running (:9222 fleet/agent)" `
    { Get-Json 'http://127.0.0.1:9222/json/version' | Out-Null; $true } `
    "launch it: powershell -File scripts\start_companion_edge.ps1   (then sign into M365 once)"

# ONE IMPLEMENTATION, NOT TWO. This used to require an m365/copilot TAB to be open, which the
# websocket-driven fleet never creates -- so a signed-in machine reported RED. The check now
# calls the same helper setup uses, which PROBES the chat page in the background and looks for a
# login WALL. "No tab" is not evidence of anything; a wall is.
#
# Exit codes: 0 signed in, 1 sign-in needed, 2 cannot tell (Edge not answering). 2 is reported as
# INFO rather than FAIL: saying "sign in" when the browser is not running sends somebody to do
# something they cannot do, and the Edge checks above already cover that case.
$signinPy = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $signinPy)) { $signinPy = "python" }
$signinScript = Join-Path $repo "scripts\ensure_m365_signin.py"
$signinCode = 2
try {
    & $signinPy $signinScript --check-only *> $null
    $signinCode = $LASTEXITCODE
} catch { $signinCode = 2 }

if ($signinCode -eq 2) {
    Check "m365_signin" "M365 sign-in state (could not be determined)" `
        { $false } `
        "the companion Edge did not answer, so this could not be checked; the Edge checks above say why" `
        -Info
} else {
    Check "m365_signin" "M365 signed in on the companion Edge" `
        { $signinCode -eq 0 } `
        "run quickstart.bat again -- it opens the sign-in window for you and waits. You only need to sign in once; it persists across restarts."
}

# 5. Bridge Edge (:9223) -- optional, only for conversation history/scrape
Check "edge_bridge" "Bridge Edge running (:9223 history/scrape) [optional]" `
    { Get-Json 'http://127.0.0.1:9223/json/version' | Out-Null; $true } `
    "optional: powershell -File scripts\start_bridge.ps1 -Keepalive   (only needed for past-conversation history)" `
    -Optional

# 5b. UI apps (CopilotChat.exe / FleetCockpit.exe) -- gitignored, so a fresh clone has neither
#     until the first build. Checked individually so the fix line names the missing one.
$copilotChatExe = Join-Path $repo "ui\CopilotChat.exe"
$fleetCockpitExe = Join-Path $repo "ui\FleetCockpit.exe"
Check "ui_copilotchat" "CopilotChat.exe built (ui\CopilotChat.exe)" `
    { Test-Path $copilotChatExe } `
    "run ui\rebuild_ui.ps1 (first build; needs .NET Framework 4.8 csc.exe, preinstalled on stock Windows 10/11)"

Check "ui_fleetcockpit" "FleetCockpit.exe built (ui\FleetCockpit.exe)" `
    { Test-Path $fleetCockpitExe } `
    "run ui\rebuild_ui.ps1 (first build; needs .NET Framework 4.8 csc.exe, preinstalled on stock Windows 10/11)"

# Bonus sub-check, only relevant when at least one UI exe is missing: is the .NET Framework 4.8
# csc.exe actually present? This tells the user which situation they are in -- a normal first
# build (csc.exe present, just hasn't been run yet) vs. a genuinely missing .NET Framework 4.8.
if (-not (Test-Path $copilotChatExe) -or -not (Test-Path $fleetCockpitExe)) {
    $cscPath = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    Check "dotnet_csc" "  (info) .NET Framework 4.8 csc.exe present ($cscPath)" `
        { Test-Path $cscPath } `
        "csc.exe not found -- enable 'Windows Features' > '.NET Framework 4.8 Advanced Services', then run ui\rebuild_ui.ps1. See docs\TROUBLESHOOTING.md ('csc.exe not found' row)" `
        -Info
}

# 6. auth end-to-end: the server ACCEPTS the Bearer on /mcp. The MCP streamable-HTTP
#    endpoint needs an initialize/session, so a bare POST returns 400/406 even when auth
#    is fine -- the right signal is "with the Bearer we are NOT rejected with 401/403"
#    (and without it we ARE), which proves the token is accepted.
function Mcp-Status([hashtable]$headers) {
    $body = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/mcp' -Method Post -Headers $headers `
             -ContentType 'application/json' -Body $body -TimeoutSec 6 -UseBasicParsing
        return [int]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode.value__ }
        return 0
    }
}
# THE FAULT THAT LEAVES EVERYTHING GREEN. MCP_UNLOCK_PASSWORD_PROTECTED is DPAPI, bound to one
# Windows account on one machine, so an .env carried from another PC holds a blob this account
# cannot open. The server starts, the Bearer check passes, doctor reports ALL GREEN -- and every
# write, run_python and shell call is refused, because there is nothing to compare against.
#
# THE LOGIC IS A FILE, NOT A `-c` PAYLOAD. Passed as @("-c", $code), Start-Process does not quote
# the element and python received only the first word -- a SyntaxError, read as "could not ask",
# converted to PASS. The first version of this check could only ever be green.
#
# BOUNDED, and every argument quoted: doctor must not hang because python did, and a path with a
# space must not become two arguments.
function Invoke-BoundedPythonFile([string]$py, [string]$script, [string[]]$scriptArgs, [int]$timeoutSec = 15) {
    $out = [System.IO.Path]::GetTempFileName()
    $err = $out + ".err"
    try {
        $argList = @(('"{0}"' -f $script))
        foreach ($a in $scriptArgs) { $argList += ('"{0}"' -f $a) }
        $p = Start-Process -FilePath $py -ArgumentList $argList -NoNewWindow -PassThru `
                           -RedirectStandardOutput $out -RedirectStandardError $err
        if (-not $p.WaitForExit($timeoutSec * 1000)) {
            try { $p.Kill() } catch { }
            return $null
        }
        $text = (Get-Content $out -Raw -ErrorAction SilentlyContinue)
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return $text.Trim()
    } catch {
        return $null
    } finally {
        Remove-Item $out, $err -Force -ErrorAction SilentlyContinue
    }
}
$script:unlockVerdict = "unknown"
$script:unlockFix = ("MCP_UNLOCK_PASSWORD_PROTECTED was written by a different Windows account " +
                     "or PC and cannot be decrypted here, so every mutating tool will be " +
                     "refused while everything else looks fine. start_all.bat re-establishes it " +
                     "automatically -- run it once, then re-run this check.")
# COULD-NOT-ASK IS INDETERMINATE, NOT A PASS. Check-TriState exists for this: $null is neither a
# security PASS nor a repairable FAIL, and it keeps the run from reporting completion on it.
Check-TriState "unlock_password_usable" "Unlock password readable by THIS Windows account" `
    {
        $py = Join-Path $repo ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { return $null }      # python_runnable already reported that
        $checker = Join-Path $scriptDir "check_unlock_usable.py"
        if (-not (Test-Path $checker)) { return $null }
        $v = Invoke-BoundedPythonFile $py $checker @($envPath)
        if ($null -eq $v) { return $null }
        $script:unlockVerdict = $v
        if ($v -eq "ok") { return $true }
        if ($v -eq "unset") {
            $script:unlockFix = ("no unlock password is configured at all, so every mutating " +
                                 "tool (write_file, run_python, shell) will be refused. Re-run " +
                                 "setup.bat, which generates one.")
            return $false
        }
        if ($v -like "error:*") { return $null }
        return $false
    } `
    $script:unlockFix

Check "auth_bearer" "Auth OK end-to-end (Bearer accepted on /mcp)" `
    {
        $key = $envv['MCP_API_KEY']; if (-not $key) { return $false }
        $withKey = Mcp-Status @{ Authorization = ("Bearer " + $key) }
        # accepted iff the server did not reject the token (not 401/403) and it actually answered
        ($withKey -ne 0) -and ($withKey -ne 401) -and ($withKey -ne 403)
    } `
    "Bearer rejected (401/403): the 'Bearer <MCP_API_KEY>' in Copilot Studio must match .env exactly; if 0, the server is down -> start_all.bat"

Write-Host ""
Write-Host "---------------------------------------------"
if ($script:bad -eq 0 -and $script:warn -eq 0) {
    Write-Host ("ALL GREEN ({0} checks). You're set." -f $script:ok) -ForegroundColor Green
    Write-Host "Daily startup: double-click the 'M365 Companion' icon on your Desktop." -ForegroundColor Green
} elseif ($script:bad -eq 0) {
    Write-Host ("{0} OK, {1} temporarily indeterminate -- re-run doctor.bat after connectivity settles." -f $script:ok, $script:warn) -ForegroundColor Yellow
} else {
    Write-Host ("{0} OK, {1} need attention, {2} indeterminate -- fix red lines, then re-run: doctor.bat" -f $script:ok, $script:bad, $script:warn) -ForegroundColor Yellow
}
Write-Host ""

if ($Json) {
    # Bind directly (not via the pipeline) so ConvertTo-Json always serializes this as
    # ONE JSON array, even in the edge case where $script:results has exactly one entry
    # (piping would unwrap it and emit a bare object instead of a 1-element array).
    Write-Output (ConvertTo-Json -InputObject $script:results -Compress)
}

# Nonzero exit for confirmed failures in BOTH modes; transient indeterminate warnings remain 0
# so automation does not treat a temporary CLI timeout as permission to repair. Additive only: no
# existing caller reads this script's exit code (doctor.bat just runs it then `pause`;
# start_all.ps1 does not invoke doctor.ps1 at all), so this cannot break anything that
# already works, and it gives scripts\repair.ps1 (and any other automation) a cheap
# "is there anything to do" signal without re-parsing -Json output.
exit $script:bad
