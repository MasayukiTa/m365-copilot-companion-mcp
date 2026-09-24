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
# COUNTED APART FROM $warn, which also holds optional components being absent -- a complete
# setup. This is required checks that could not be determined, which is not one.
$script:unknown = 0
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
        # Check-TriState is used only for REQUIRED checks, so every indeterminate here is one.
        $script:unknown++
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
# IS IT ACTUALLY AN EDGE DEBUG PORT? The callers fetched /json/version and discarded it, so
# anything answering that URL with any JSON passed -- and the check's name claims a browser.
# Measured, the real endpoint carries Browser="Edg/..." and a webSocketDebuggerUrl; a static
# document that happens to be JSON carries neither.
function Test-EdgeCdp([int]$port) {
    try {
        $v = Get-Json ("http://127.0.0.1:" + $port + "/json/version")
    } catch {
        return $false
    }
    if (-not $v) { return $false }
    return (("" + $v.Browser) -like "Edg*") -and (("" + $v.webSocketDebuggerUrl) -like "ws://*")
}

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
    # SCOPED, because a bare match on the file name finds anything that MENTIONS it -- measured:
    # four matches here, three of them shell commands that merely contained the string. -like
    # rather than -match so a path of backslashes needs no escaping.
    $supPath = Join-Path $scriptDir "supervisor.ps1"
    try {
        $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
             Where-Object {
                 $_.CommandLine -and
                 ($_.Name -match '^(powershell|pwsh)') -and
                 ($_.CommandLine -like ("*" + $supPath + "*")) -and
                 ($_.CommandLine -notlike "*register-supervisor*")
             } |
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
# Test-GeneratedTunnelName (a name setup_devtunnel.ps1 GENERATES: its fixed default plus an
# optional hex machine suffix) now lives in tunnel_name_util.ps1, dot-sourced near the top of
# this file -- it used to be duplicated here (byte-for-byte except its name) as
# Test-GeneratedTunnelNameDoctor, hand-kept in sync with setup_devtunnel.ps1's copy and with
# bootstrap.py's _is_generated_tunnel_name (D29: without this exemption, a user named e.g.
# "pan" or "com" had every run's generated name -- "m365-copilot-companion-<hex>" -- flagged
# as identifying, even though nothing about it was). Test-IdentifyingTunnelName below still
# stays duplicated between this file and setup_devtunnel.ps1 by hand (and with bootstrap.py's
# _is_identifying_tunnel_name) -- only the generated-name exemption moved.
function Test-IdentifyingTunnelName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $false }
    $lower = $name.ToLowerInvariant()
    if ((Get-Sha256HexDoctor $lower) -eq $FULLNAME_SHA256) { return $true }
    $tokens = @($lower -split '[^a-z0-9]+' | Where-Object { $_ })
    foreach ($t in $tokens) {
        if ((Get-Sha256HexDoctor $t) -eq $TOKEN_SHA256) { return $true }
    }
    # NOT FOR A GENERATED NAME (D29) -- see tunnel_name_util.ps1's Test-GeneratedTunnelName.
    if (Test-GeneratedTunnelName $name) { return $false }
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
    # NOTHING TO OWN IS NOT OWNERSHIP. Returning true here reported "owned by this account" for
    # a machine with no tunnel name configured at all -- and this check is not part of the
    # tunnel chain, so it is not skipped when the name is missing. $null is the contract this
    # function already has for "could not be determined", which is exactly what this is.
    if ([string]::IsNullOrWhiteSpace($name)) { return $null }
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

# 3c.1 The tunnel's ACCESS GRANT -- what a remote caller is allowed through with (D15 in the
# 2026-09-24 new-PC review). Nothing checked it: quickstart could print SETUP COMPLETE over a
# tunnel with NO grant (choice N), which no remote client can pass, or over an anonymous one
# the operator believed closed. Read-only: `devtunnel access list` for the tunnel and for port
# 8000. MEASURED on the working machine, an allow entry prints as "+Anonymous [connect]"; the
# tenant entry's text has not been observed, so "+Tenant"/"+Tenants" is matched. This parse is
# setup_devtunnel.ps1's Get-AccessGrantFromListing, and
# scripts/test_setup_devtunnel_access_and_identity.py runs both on the same listings.
function Test-AnonymousInListing([string]$text) { return [bool]($text -match '\+\s*Anonymous\b') }
function Test-TenantInListing([string]$text) { return [bool]($text -match '\+\s*Tenants?\b') }
function Get-AccessGrantFromListing([string]$text) {
    if (Test-AnonymousInListing $text) { return "anonymous" }
    if (Test-TenantInListing $text) { return "tenant" }
    return "none"
}
# Like Invoke-DevTunnelBounded, but also returns the exit code: an error message ("tunnel not
# found", "unauthorized") contains no "+Anonymous" and must not be read as "no grant".
function Invoke-DevTunnelBoundedExit([string[]]$dtArgs, [int]$timeoutSec) {
    try {
        $job = Start-Job -ScriptBlock {
            param($exe, $a)
            $o = ""
            try { $o = (& $exe @a 2>&1 | Out-String) } catch { $o = "" }
            [PSCustomObject]@{ Out = $o; Exit = $LASTEXITCODE }
        } -ArgumentList $DevTunnel, $dtArgs
    } catch { return $null }
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ($job.State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 150 }
    if ($job.State -eq 'Running') {
        try { Stop-Job $job -ErrorAction SilentlyContinue } catch { }
        try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
        return $null
    }
    $r = Receive-Job $job | Select-Object -Last 1
    try { Remove-Job $job -Force -ErrorAction SilentlyContinue } catch { }
    return $r
}
function Get-TunnelAccessGrantDoctor([string]$name, [int]$port) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $null }
    $a = Invoke-DevTunnelBoundedExit @('access', 'list', $name) 10
    $b = Invoke-DevTunnelBoundedExit @('access', 'list', $name, '-p', [string]$port) 10
    if ((-not $a) -or (-not $b) -or ($a.Exit -ne 0) -or ($b.Exit -ne 0)) { return $null }
    return (Get-AccessGrantFromListing ($a.Out + "`n" + $b.Out))
}
$script:tunnelAccess = $null
$accessId = "tunnel_access"
$accessFixAnon = "Anonymous is what an API-key Copilot Studio connector needs. To close it instead, run quickstart.bat and choose N (no remote access) -- setup_devtunnel.ps1 now removes the anonymous grant."
$accessFixTenant = "Tenant-only: a caller must present an Entra sign-in of your tenant to the tunnel. NOT VERIFIED that a Copilot Studio connector using an API key can do that -- if its connection test fails, run quickstart.bat and choose A."
$accessFixNone = "No access grant: nothing remote can connect. Run quickstart.bat and choose A (anonymous, gated by the Bearer token) or T (tenant)."
if ($script:tunnelChainBroken) {
    Write-Host ("  [SKIP] Dev Tunnel access grant") -ForegroundColor DarkGray
    Write-Host ("         blocked by the Dev Tunnel check above -- fix that first, then re-run doctor") -ForegroundColor DarkGray
    Add-Result $accessId $false "Dev Tunnel access grant" $accessFixNone $false $false $true
} else {
    $script:tunnelAccess = Get-TunnelAccessGrantDoctor $tname 8000
    switch ($script:tunnelAccess) {
        "anonymous" {
            $n = "Dev Tunnel access grant: anonymous (anyone with the URL reaches the server; the Bearer token is the only gate)"
            Add-Result $accessId $true $n $accessFixAnon $false $false $false
            Write-Host ("  [ OK ] " + $n) -ForegroundColor Green
            $script:ok++
        }
        "tenant" {
            # A REQUIRED property -- can Copilot Studio get through? -- that cannot be established
            # here, so counted as unknown: quickstart must not call that a complete setup.
            $n = "Dev Tunnel access grant: tenant only (Copilot Studio with an API key is not expected to pass -- unverified)"
            Add-Result $accessId $false $n $accessFixTenant $false $false $false $true
            Write-Host ("  [WARN] " + $n) -ForegroundColor Yellow
            Write-Host ("         " + $accessFixTenant) -ForegroundColor DarkYellow
            $script:warn++
            $script:unknown++
        }
        "none" {
            $n = "Dev Tunnel access grant: none (no remote client can connect)"
            Add-Result $accessId $false $n $accessFixNone $false $false $false
            Write-Host ("  [FAIL] " + $n) -ForegroundColor Red
            Write-Host ("         fix: " + $accessFixNone) -ForegroundColor Yellow
            $script:bad++
        }
        default {
            $n = "Dev Tunnel access grant (could not be read: devtunnel access list failed or timed out)"
            Add-Result $accessId $false $n ("retry doctor.bat; or run: devtunnel access list " + $tname) $false $false $false $true
            Write-Host ("  [WARN] " + $n) -ForegroundColor Yellow
            $script:warn++
            $script:unknown++
        }
    }
}

# 3c.2 The public URL must reach THIS server, not merely answer (D15).
# A 200 IS NOT THE SERVER. Invoke-WebRequest follows redirects, and a tunnel that does not admit
# anonymous callers answers with the dev tunnels sign-in page or the relay's browser warning
# page -- both HTML, both able to end in 200 -- so this check could pass while no remote client
# could reach anything (UNVERIFIED which page a given relay serves; the check no longer depends
# on it). What main.py's /health returns is JSON with status "ok" and the serving process's
# server_pid (main.py health(); scripts/status.py compares the pid the same way). So: the
# answer must come from the tunnel's own host, be that JSON, and -- when the local server
# answers too -- carry THIS machine's pid; a different pid means another machine hosts the same
# tunnel and the relay is splitting the calls between them (D7). The anti-phishing header is
# the documented way for a non-browser client to skip the relay's warning page; Copilot Studio
# (server-to-server) never sees that page.
function Test-TunnelHealthAnswer([string]$tunnelUrl, $localPid, [int]$timeoutSec = 7) {
    $res = [PSCustomObject]@{ Ok = $false; Why = "" }
    if (-not $tunnelUrl) { $res.Why = "MCP_TUNNEL_URL is not set in .env"; return $res }
    # MCP_TUNNEL_URL points at the /mcp path (e.g. https://host.devtunnels.ms/mcp);
    # /health is a SIBLING route at the tunnel origin, not nested under /mcp -- so
    # naively appending "/health" to $turl produced .../mcp/health, a 404 that made
    # this check FAIL even when the tunnel was being served correctly. Use the
    # origin (scheme+host) instead.
    $u = [Uri]$tunnelUrl
    $origin = $u.GetLeftPart([UriPartial]::Authority)
    try {
        $r = Invoke-WebRequest -Uri ($origin + '/health') -TimeoutSec $timeoutSec -UseBasicParsing `
                               -Headers @{ 'X-Tunnel-Skip-AntiPhishing-Page' = 'true' }
    } catch {
        $code = $null
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code) {
            $res.Why = "the public URL answered HTTP $code instead of the server's /health -- the tunnel relay refused the call (a tunnel without an anonymous grant does this to every caller without an Entra sign-in)"
        } else {
            $res.Why = "no answer from the public URL: " + $_.Exception.Message
        }
        return $res
    }
    $finalUri = $null
    try { $finalUri = $r.BaseResponse.ResponseUri } catch { }
    if (-not $finalUri) { try { $finalUri = $r.BaseResponse.RequestMessage.RequestUri } catch { } }
    if ($finalUri -and ($finalUri.Host -ne $u.Host)) {
        $res.Why = "the public URL redirected to " + $finalUri.Host + " (a sign-in page), not the server -- the tunnel does not admit anonymous callers"
        return $res
    }
    $j = $null
    try { $j = ($r.Content | ConvertFrom-Json) } catch { $j = $null }
    if ((-not $j) -or ($j.status -ne "ok")) {
        $head = ("" + $r.Content)
        if ($head.Length -gt 60) { $head = $head.Substring(0, 60) }
        $head = ($head -replace '\s+', ' ')
        $res.Why = "HTTP " + $r.StatusCode + " but not the server's /health JSON (it began: '" + $head + "') -- the dev tunnel relay answered (sign-in or warning page), not this server"
        return $res
    }
    if ($localPid -and $j.server_pid -and ([string]$j.server_pid -ne [string]$localPid)) {
        $res.Why = "the public URL reached server pid " + $j.server_pid + ", but this machine's server is pid " + $localPid + " -- another machine also hosts this tunnel and the relay splits the calls between them. Stop the stack on the other machine, or give this one its own tunnel: powershell -File scripts\setup_devtunnel.ps1"
        return $res
    }
    $res.Ok = $true
    return $res
}
$script:tunnelServingWhy = ""
TunnelCheck "tunnel_serving" "Dev Tunnel host serving (public URL -> THIS server's /health)" `
    {
        $localPid = $null
        try { $localPid = (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).server_pid } catch { }
        $probe = Test-TunnelHealthAnswer $turl $localPid
        $script:tunnelServingWhy = $probe.Why
        $probe.Ok
    } `
    "the tunnel exists but is not being served -- run start_all.bat (the supervisor hosts it). If this stays red while the checks above are green, MCP_TUNNEL_URL in .env may be stale -- compare it to the URL shown by 'devtunnel show <name>'."
# WHAT THE SUPERVISOR CONCLUDED. When another PC hosts this PC's tunnel, the probe above sees only
# a timeout ("no answer from the public URL") -- measured 2026-09-24 -- which reads like "not
# hosted" and sends the operator to start_all, i.e. to re-host, which is the fight the supervisor
# now refuses. The supervisor can tell the two apart (the relay's host count vs. a host process
# of this machine) and writes its verdict to .fleet\tunnel_host.json. Trusted only while the
# supervisor that wrote it is alive, so a file left by a dead one cannot speak for the present.
function Get-SupervisorTunnelVerdict {
    try {
        $p = Join-Path $repo ".fleet\tunnel_host.json"
        if (-not (Test-Path -LiteralPath $p)) { return $null }
        $j = Get-Content -LiteralPath $p -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if (-not $j -or -not $j.supervisor_pid) { return $null }
        $sp = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$j.supervisor_pid) -ErrorAction SilentlyContinue
        if (-not $sp -or ([string]$sp.CommandLine) -notlike "*supervisor.ps1*") { return $null }
        return $j
    } catch { return $null }
}
$script:supTunnel = Get-SupervisorTunnelVerdict
if ($script:supTunnel -and ($script:supTunnel.state -eq "foreign" -or $script:supTunnel.state -eq "shared")) {
    $lr = $script:results[-1]
    if ($lr.id -eq "tunnel_serving" -and -not $lr.skipped) {
        $probeWhy = $script:tunnelServingWhy
        $script:tunnelServingWhy = ([string]$script:supTunnel.message + " To fix: " + [string]$script:supTunnel.action)
        if ($probeWhy) { $script:tunnelServingWhy += (" [the probe itself saw: " + $probeWhy + "]") }
        if ($lr.ok) {
            # The probe got through (to this PC, this time) but the supervisor sees another host:
            # say so, without turning a passing check red on the supervisor's word alone.
            Write-Host ("         note (supervisor): " + $script:tunnelServingWhy) -ForegroundColor Yellow
        }
    }
}

# SAY WHY. The fixed advice above is for "not hosted"; a relay page or a second host needs a
# different action, and the probe knows which it saw.
$lastResult = $script:results[-1]
if ($lastResult.id -eq "tunnel_serving" -and -not $lastResult.ok -and -not $lastResult.skipped -and $script:tunnelServingWhy) {
    Write-Host ("         why: " + $script:tunnelServingWhy) -ForegroundColor Yellow
    if ($script:tunnelAccess -and $script:tunnelAccess -ne "anonymous") {
        Write-Host ("         the tunnel's access grant is '" + $script:tunnelAccess + "' (see the access check above)") -ForegroundColor Yellow
    }
    $lastResult.fix = $script:tunnelServingWhy + " | " + $lastResult.fix
}

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
# SAY WHAT IT SAID, not just "launch it". start_companion_edge.ps1 now redirects its launch
# stderr to .setup\logs\companion_edge_<profile>.err.log, so when :9222 does not answer we can
# show the actual reason (a locked profile, a bad --user-data-dir, an Edge that refused the
# debugging port) instead of pointing the operator back at the command that just failed. Same
# shape as the supervisor advice above.
$script:edgeErrLog = Join-Path $repo ".setup\logs\companion_edge_copilot-companion-edge.err.log"
$script:edgeFix = "launch it: powershell -File scripts\start_companion_edge.ps1   (then sign into M365 once)"
if (-not (Test-EdgeCdp 9222)) {
    try {
        if (Test-Path $script:edgeErrLog) {
            $edgeLines = @(Get-Content $script:edgeErrLog -Tail 10 -ErrorAction Stop |
                           Where-Object { $_.Trim() })
            if ($edgeLines.Count -gt 0) {
                $script:edgeFix = ("the companion Edge was launched and it reported:" +
                                   [Environment]::NewLine + "           " +
                                   ($edgeLines -join ([Environment]::NewLine + "           ")) +
                                   [Environment]::NewLine +
                                   "           after fixing that, re-run: powershell -File scripts\start_companion_edge.ps1")
            }
        }
    } catch { }
}
Check "edge_companion" "Companion Edge running (:9222 fleet/agent)" `
    { Test-EdgeCdp 9222 } `
    $script:edgeFix

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
# READ THE VERDICT, NOT THE EXIT CODE. 1 means "I looked and found a sign-in wall" -- and a
# traceback also exits 1. This branch mapped everything that was neither 0 nor 2 onto
# "M365 signed in: FAIL -- run quickstart.bat again", so any unexpected exception in the
# checker became a confident instruction to redo a sign-in that may be perfectly fine. The
# operator hit exactly that contradiction on 2026-09-22: quickstart printed "[ OK ] signed in.
# Continuing." and this line then said FAIL.
#
# ensure_m365_signin --check-only now prints "VERDICT: signed_in|sign_in_needed|cannot_tell"
# as its last line. A missing verdict means the checker did not get far enough to have one,
# which is "could not tell" -- never "not signed in".
$signinCode = 2
$signinOut = ""
try {
    $signinOut = (& $signinPy $signinScript --check-only --all 2>&1 | Out-String)
    $signinCode = $LASTEXITCODE
} catch { $signinCode = 2; $signinOut = "" }

$signinVerdict = "cannot_tell"
if ($signinOut -match "VERDICT:\s*(\w+)") { $signinVerdict = $Matches[1] }

if ($signinVerdict -eq "cannot_tell") {
    $detail = if ($signinOut -match "VERDICT:") {
        "the companion Edge did not answer, so this could not be checked; the Edge checks above say why"
    } else {
        "the sign-in checker did not report a verdict (exit $signinCode) -- it failed before deciding, which is NOT the same as 'not signed in'. Run it directly to see why: python scripts\ensure_m365_signin.py --check-only"
    }
    Check "m365_signin" "M365 sign-in state (could not be determined)" `
        { $false } `
        $detail `
        -Info
} else {
    # CARRY THE REASON ONTO THE SCREEN. The fix line alone ("run quickstart.bat again") is the
    # same sentence whether a real login page is open or something else went wrong, and on
    # 2026-09-23 a repeat of this FAIL arrived as a screenshot with nothing on it to act on --
    # the checker knew WHICH tab was the wall and the doctor was throwing that away.
    $signinWhy = ""
    if ($signinOut -match "(?m)^\s*sign-in needed \((.+)\)\s*$") { $signinWhy = $Matches[1] }
    $signinFix = "run quickstart.bat again -- it opens the sign-in window for you and waits. You only need to sign in once; it persists across restarts."
    if ($signinWhy) { $signinFix = "$signinWhy. $signinFix" }
    Check "m365_signin" "M365 signed in on the companion Edge" `
        { $signinVerdict -eq "signed_in" } `
        $signinFix
}

# EVERY MANAGED EDGE, NOT JUST :9222. Each one runs on its OWN --user-data-dir -- Edge locks a
# profile to a single process, so concurrent browsers REQUIRE distinct profiles -- and a
# distinct profile is a distinct cookie jar. Signing in on the companion signs in the companion
# and nothing else, and until 2026-09-23 nothing here looked at the others: the "Bridge Edge
# running (:9223)" row above says the process answers CDP, not that it can reach Copilot.
#
# The rows are built from what the checker prints, and the checker takes its port list from
# relay.edge_recover.MANAGED_EDGE_PROFILES. No list of ports lives in this file; that constant's
# docstring records four separate outages caused by exactly such a copy.
#
# THE BRIDGE'S BROWSER IS NOT OPTIONAL. This row said "(optional)" for every non-primary profile,
# while start_all counted the same sign-in as a startup problem -- one screen said WARN and the
# next said FAIL about one fact. The code decides it: every chat-window turn runs on the bridge
# Edge's session (bridge/copilot_bridge.py drives the page, and captures the socket token, from
# the CDP context on MCP_BRIDGE_CDP_PORT), and "Chat backend serving" below is a REQUIRED check.
# A backend that is up but signed out answers nothing. So the bridge's row is required; any other
# profile (the evaluation browser) stays optional. The port is the one start_bridge.ps1 and the
# bridge read: MCP_BRIDGE_CDP_PORT, else 9223.
#
# THE FIX LINE IS FOR THE PERSON AT THE DESK, NOT FOR AN ENGINEER. It was a PowerShell command.
# start_bridge.ps1's supervisor now brings that window to the front by itself when sign-in is
# needed, so what the person has to do is sign in in it -- said in Japanese. Written as code
# points because this file is ASCII (Windows PowerShell 5.1 reads a BOM-less file as the ANSI
# code page, which turns UTF-8 Japanese into mojibake).
$bridgeCdpPort = "9223"
if ($env:MCP_BRIDGE_CDP_PORT) { $bridgeCdpPort = $env:MCP_BRIDGE_CDP_PORT }
elseif ($envv["MCP_BRIDGE_CDP_PORT"]) { $bridgeCdpPort = $envv["MCP_BRIDGE_CDP_PORT"] }
# Reads: "The Edge the chat window uses is stopped on a sign-in page. When sign-in is needed that
# Edge comes to the front by itself. Sign in with your work account in the Edge that appeared. If
# it is not showing, start 'M365 Companion' on the Desktop again and it will appear."
# (No Japanese literal even in this comment: a UTF-8 byte read as a cp932 lead byte can swallow
# the newline that ends the comment.)
$bridgeSigninFixJa = -join (@(
    0x30C1,0x30E3,0x30C3,0x30C8,0x753B,0x9762,0x304C,0x4F7F,0x3046,0x0020,0x0045,0x0064,
    0x0067,0x0065,0x0020,0x304C,0x30B5,0x30A4,0x30F3,0x30A4,0x30F3,0x753B,0x9762,0x3067,
    0x6B62,0x307E,0x3063,0x3066,0x3044,0x307E,0x3059,0x3002,0x30B5,0x30A4,0x30F3,0x30A4,
    0x30F3,0x304C,0x5FC5,0x8981,0x306B,0x306A,0x308B,0x3068,0x3001,0x305D,0x306E,0x0020,
    0x0045,0x0064,0x0067,0x0065,0x0020,0x304C,0x81EA,0x52D5,0x3067,0x524D,0x9762,0x306B,
    0x8868,0x793A,0x3055,0x308C,0x307E,0x3059,0x3002,0x8868,0x793A,0x3055,0x308C,0x305F,
    0x0020,0x0045,0x0064,0x0067,0x0065,0x0020,0x3067,0x3001,0x4F1A,0x793E,0x306E,0x30A2,
    0x30AB,0x30A6,0x30F3,0x30C8,0x3067,0x30B5,0x30A4,0x30F3,0x30A4,0x30F3,0x3057,0x3066,
    0x304F,0x3060,0x3055,0x3044,0x3002,0x8868,0x793A,0x3055,0x308C,0x3066,0x3044,0x306A,
    0x3044,0x5834,0x5408,0x306F,0x3001,0x30C7,0x30B9,0x30AF,0x30C8,0x30C3,0x30D7,0x306E,
    0x300C,0x004D,0x0033,0x0036,0x0035,0x0020,0x0043,0x006F,0x006D,0x0070,0x0061,0x006E,
    0x0069,0x006F,0x006E,0x300D,0x3092,0x3082,0x3046,0x4E00,0x5EA6,0x8D77,0x52D5,0x3059,
    0x308B,0x3068,0x8868,0x793A,0x3055,0x308C,0x307E,0x3059,0x3002
) | ForEach-Object { [char]$_ })
foreach ($line in ($signinOut -split "`r?`n")) {
    $m = [regex]::Match($line, '^\s*PROFILE:\s+(\d+)\s+(\S+)\s+(\w+)(\s+\[primary\])?\s*\((.*)\)\s*$')
    if (-not $m.Success) { continue }
    if ($m.Groups[4].Success -and $m.Groups[4].Value) { continue }   # the primary has its own row above
    $pPort = $m.Groups[1].Value
    $pName = $m.Groups[2].Value
    $pVerdict = $m.Groups[3].Value
    if ($pVerdict -eq "cannot_tell") { continue }   # not running, or no page to judge from
    if ($pPort -eq $bridgeCdpPort) {
        Check "m365_signin_$pPort" "M365 signed in on the chat bridge Edge $pName (:$pPort) -- the chat window needs it" `
            { $pVerdict -eq "signed_in" } `
            ($bridgeSigninFixJa + " (" + $m.Groups[5].Value + ")")
    } else {
        Check "m365_signin_$pPort" "M365 signed in on $pName (:$pPort)" `
            { $pVerdict -eq "signed_in" } `
            "$($m.Groups[5].Value). This is a SEPARATE browser profile from the companion Edge, so signing in there did not sign in here: powershell -File scripts\start_companion_edge.ps1 -Port $pPort -Profile $pName -Foreground -Url https://m365.cloud.microsoft/chat" `
            -Optional
    }
}

# 5. Bridge Edge (:9223) -- REQUIRED, not optional. This row used to say "[optional] ...
#    history/scrape", from when the bridge's Edge was only used to scrape past-conversation
#    history. It is not any more: every chat-window turn now runs on this Edge's CDP session
#    (bridge/copilot_bridge.py drives the page, and captures the socket token, from the
#    context on MCP_BRIDGE_CDP_PORT), so a doctor run that called this optional while
#    "Chat backend serving" below (bridge_backend, required) and start_all's own summary both
#    counted a down bridge Edge as a real problem was one fact reported two different ways on
#    one screen. repair.ps1's registry entry for this same id (Key edge_bridge) already runs
#    start_bridge.ps1 -Keepalive without calling it optional; this label and requiredness now
#    say the same thing.
Check "edge_bridge" "Bridge Edge running (:9223 -- the chat window's browser)" `
    { Test-EdgeCdp 9223 } `
    "powershell -File scripts\start_bridge.ps1 -Keepalive"

# 4c. The chat backend itself. edge_bridge above probes the Edge the bridge DRIVES; this is
#     the HTTP server CopilotChat talks to, and nothing looked at it -- so a bridge that holds
#     the port without serving read as ALL GREEN. Seen doing exactly that: / answered 200 while
#     /conv dropped the connection, because the process holding :8765 had been started with the
#     system python instead of the venv's and could not import what it needs.
#
#     /conv, not /, because /conv is the endpoint start_all itself uses to decide whether the
#     bridge is alive -- probing / would have passed here and called a broken chat backend fine.
Check "bridge_backend" "Chat backend serving (:8765 /conv)" `
    {
        try {
            $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/conv' -TimeoutSec 6 -UseBasicParsing
            [int]$r.StatusCode -lt 500
        } catch {
            # An HTTP error status still means something is serving; a dropped or refused
            # connection does not.
            if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode.value__ -lt 500 }
            else { $false }
        }
    } `
    "the chat backend is not answering on :8765. If a process holds the port but does not serve, it was probably started outside the venv (check the command line of the owner of :8765): stop it, then run start_all.bat, which relaunches the bridge with the venv interpreter."

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

# STALE RUNNING SERVER. A `git pull` lands new relay/tools/main.py on disk, but a server
# that is already running keeps executing the code it imported at startup -- /health still
# answers 200 and every other check is green, so the checkout and the live process silently
# disagree until the next real restart. supervisor.ps1 records the SHA each server starts on
# in .setup\server_started_head.txt; here we compare it to the checkout's HEAD. The decision
# itself lives in scripts\stale_server_check.py (pure, pytest-covered) so this check cannot
# drift from what the tests assert. Tri-state: "stale" is red, "current" is green, and a
# missing/unreadable marker ("unknown") is indeterminate -- never a silent pass.
Check-TriState "server_not_stale" "Running server is on the current checkout (not stale after an update)" `
    {
        # Only meaningful when a server is actually up; server_up above already reports the down case.
        $up = $false
        try { $up = (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } catch { $up = $false }
        $py = Join-Path $repo ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { return $null }
        $checker = Join-Path $scriptDir "stale_server_check.py"
        if (-not (Test-Path $checker)) { return $null }
        $marker = Join-Path $repo ".setup\server_started_head.txt"
        $head = (& git -C $repo rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $head) { return $null }
        $runningFlag = if ($up) { "1" } else { "0" }
        $v = Invoke-BoundedPythonFile $py $checker @($marker, $head.Trim(), $runningFlag)
        if ($null -eq $v) { return $null }
        $script:staleVerdict = $v
        switch ($v) {
            "current"   { return $true }
            "no_server" { return $true }   # nothing running is server_up's concern, not this one
            "stale"     { return $false }
            default     { return $null }   # "unknown" / "error:*" -> indeterminate
        }
    } `
    ("the running MCP server started on a different commit than the checkout is on now -- it is " +
     "executing code an update has already replaced on disk. Restart it so the new code takes effect: " +
     "close it (or let supervisor.ps1 cycle it) and re-run start_all.bat.")

Check "auth_bearer" "Auth OK end-to-end (Bearer accepted on /mcp)" `
    {
        $key = $envv['MCP_API_KEY']; if (-not $key) { return $false }
        $withKey = Mcp-Status @{ Authorization = ("Bearer " + $key) }
        # MARKED AS doctor's OWN. main.py counts a marked, loopback, unforwarded rejection as
        # self_test_rejections_10m instead of auth_fail_10m: this probe is refused on purpose on
        # every run, and counting it as a key mismatch turned the cockpit's server dot amber
        # ("suspect MCP_API_KEY mismatch") from doctor alone -- auth_fail_10m measured 14-16.
        $noKey = Mcp-Status @{ 'X-MCP-Self-Test' = 'doctor' }
        $script:authWithKey = $withKey
        $script:authNoKey = $noKey
        # MEASURED against this server: a correct key answers 400 -- the probe body is not a full
        # MCP handshake, and auth ran first and passed -- no key answers 401, and a bogus path
        # answers 404. So requiring 200 would be wrong, but "anything that is not 401" was far
        # too weak: 404, 405 and 500 all passed it.
        #
        # AND A SERVER WITH AUTH TURNED OFF PASSED TOO, because nothing asked what happens
        # WITHOUT the key -- which is the only observation that says anything about auth.
        # 5xx was still passing: excluding 0/401/403/404 one at a time left server errors in.
        # A range is the right shape -- anything outside 2xx-4xx is not an authenticated answer.
        ($withKey -ge 200) -and ($withKey -lt 500) `
            -and ($withKey -ne 401) -and ($withKey -ne 403) -and ($withKey -ne 404) `
            -and (($noKey -eq 401) -or ($noKey -eq 403))
    } `
    "the /mcp endpoint did not behave like an authenticated one. 0 = the server is down (start_all.bat); 401/403 with the key = the 'Bearer <MCP_API_KEY>' in Copilot Studio does not match .env; 404 = the server is answering but /mcp is not there; anything else without the key NOT being refused means authentication is not being enforced."

# 7. Last background start (.setup\logs\start_all_summary.txt). The daily launchers
# (start_all.bat, the Desktop icon, the Startup .lnk/Task) run start_all.ps1 through
# start_all_hidden.vbs / start_background_hidden.vbs -- window 0, exit code unread -- so a
# failure there used to produce nothing anyone would see until the chat window quietly
# stopped answering. start_all.ps1 now rewrites this file on EVERY run, clean or not (see
# its own header comment above Write-StartupSummary), in exactly this shape:
#   failures=N
#   when=yyyy-MM-dd HH:mm:ss
#   mode=<full|core (-CoreOnly)|background (-NoUi)>
#   - <fix text for failure 1>          (N of these, only when N>0)
#   fix: run doctor.bat for the specific fix for each line   (only when N>0)
# doctor only READS this file -- it never runs start_all.ps1 or repairs anything itself.
function Get-LastStartSummaryDoctor([string]$Path) {
    # Returns $null when the file is absent, empty or unreadable; otherwise a hashtable:
    #   Failures [int]                  the recorded count (0 if the line is missing/bad)
    #   When     [Nullable[datetime]]   parsed "when=" value, or $null if missing/unparsable
    #   Mode     [string]               the recorded "mode=" value, or ""
    #   Lines    [string[]]             the "- " fix lines, in the order start_all wrote them
    #   FileTime [Nullable[datetime]]   the file's own last-write time -- the staleness
    #                                   fallback for when "when=" cannot be parsed
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = $null
    # -Encoding UTF8: start_all writes this file as UTF-8 without a BOM, which Windows PowerShell
    # 5.1 would otherwise read as the ANSI code page -- any non-ASCII line would come out garbled.
    try { $raw = @(Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction Stop) } catch { return $null }
    if (-not $raw -or $raw.Count -eq 0) { return $null }
    $failures = 0
    $when = $null
    $mode = ""
    $lines = @()
    foreach ($ln in $raw) {
        if ($ln -match '^failures=(\d+)$') {
            $failures = [int]$matches[1]
        } elseif ($ln -match '^when=(.+)$') {
            $parsed = [datetime]::MinValue
            if ([datetime]::TryParse($matches[1], [ref]$parsed)) { $when = $parsed }
        } elseif ($ln -match '^mode=(.*)$') {
            $mode = $matches[1]
        } elseif ($ln.StartsWith("- ")) {
            $lines += $ln.Substring(2)
        }
    }
    $fileTime = $null
    try { $fileTime = (Get-Item -LiteralPath $Path -ErrorAction Stop).LastWriteTime } catch { }
    return @{ Failures = $failures; When = $when; Mode = $mode; Lines = $lines; FileTime = $fileTime }
}
# PURE decision: does a recorded start no longer speak for the CURRENT boot? Older than 24h
# on the wall clock, or older than the currently running server's own start time -- the
# latter means the server has restarted since this record was written, so either a start_all
# run happened and (for some other reason) did not rewrite the file, or the server was
# started some other way; either way the record predates what is running now. Either input
# may be $null (unknown); a $null SummaryTime is treated as not-stale -- nothing to compare
# (Get-LastStartSummaryDoctor already falls back to the file's own write time, so this is
# normally $null only when the file itself could not be stat'd).
function Test-LastStartSummaryStale {
    param($SummaryTime, $ServerStartTime, [datetime]$Now)
    if ($null -eq $SummaryTime) { return $false }
    if ($SummaryTime -lt $Now.AddHours(-24)) { return $true }
    if (($null -ne $ServerStartTime) -and ($SummaryTime -lt $ServerStartTime)) { return $true }
    return $false
}

$script:lastStartPath = Join-Path $repo ".setup\logs\start_all_summary.txt"
$script:lastStart = Get-LastStartSummaryDoctor $script:lastStartPath
if (-not $script:lastStart) {
    Check "last_start" "Last background start recorded (.setup\logs\start_all_summary.txt)" `
        { $false } `
        "no background start has been recorded on this machine yet. It is written by start_all.ps1 (the daily launcher) on every run -- double-click the 'M365 Companion' Desktop icon, or run start_all.bat once, then re-run doctor.bat." `
        -Info
} else {
    $lsWhen = $script:lastStart.When
    $lsSummaryTime = if ($lsWhen) { $lsWhen } else { $script:lastStart.FileTime }
    $lsWhenText = if ($lsWhen) {
        $lsWhen.ToString("yyyy-MM-dd HH:mm:ss")
    } elseif ($script:lastStart.FileTime) {
        $script:lastStart.FileTime.ToString("yyyy-MM-dd HH:mm:ss") + " (file time -- the recorded 'when=' line could not be read)"
    } else {
        "unknown time"
    }
    $lsModeText = if ($script:lastStart.Mode) { $script:lastStart.Mode } else { "unknown mode" }

    # THE RUNNING SERVER'S OWN START TIME, not this doctor process's. /health already reports
    # server_pid for the tunnel_serving check above; reused here so a live server that has
    # restarted since the recorded start is what "stale" is measured against.
    $lsServerStart = $null
    try {
        $lsPid = (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).server_pid
        if ($lsPid) { $lsServerStart = (Get-Process -Id ([int]$lsPid) -ErrorAction Stop).StartTime }
    } catch { }
    $lsStale = Test-LastStartSummaryStale $lsSummaryTime $lsServerStart (Get-Date)
    $lsStaleSuffix = if ($lsStale) { " [may be stale: older than 24h, or than the running server]" } else { "" }

    if ($script:lastStart.Failures -le 0) {
        Check "last_start" ("Last background start OK (" + $lsWhenText + ", " + $lsModeText + ")" + $lsStaleSuffix) `
            { $true } `
            "no problems were recorded in the last background start."
    } elseif ($script:lastStart.Lines.Count -gt 0) {
        $lsN = 0
        foreach ($lsLine in $script:lastStart.Lines) {
            $lsN++
            # THE SAME SENTENCE AS THE ROW ABOVE FOR THE SAME FACT. start_all records the bridge's
            # sign-in as "M365 sign-in needed on <profile> (:<port>)"; shown raw, the person got a
            # WARN with a command above and a FAIL with a fragment here, about one sign-in.
            $lsFix = $lsLine
            $lsM = [regex]::Match($lsLine, '^M365 sign-in needed on \S+ \(:(\d+)\)')
            if ($lsM.Success -and $lsM.Groups[1].Value -eq $bridgeCdpPort) {
                $lsFix = $bridgeSigninFixJa + " (" + $lsLine + ")"
            }
            Check ("last_start_" + $lsN) `
                ("Last background start (" + $lsWhenText + ", " + $lsModeText + ") problem " + $lsN + " of " + $script:lastStart.Failures + $lsStaleSuffix) `
                { $false } `
                $lsFix
        }
    } else {
        # failures=N>0 but no "- " lines were readable (older/corrupt file format) -- still
        # say a problem was recorded rather than going silent about it.
        Check "last_start" ("Last background start (" + $lsWhenText + ", " + $lsModeText + ") recorded " + $script:lastStart.Failures + " problem(s), but none could be read from the file" + $lsStaleSuffix) `
            { $false } `
            "run doctor.bat for the specific fix for each line; if this keeps happening, .setup\logs\start_all_summary.txt may be corrupt -- check its contents by hand."
    }
}

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
# THE COUNTS GO WHERE A CALLER CAN READ THEM. The exit code is the number of failures and
# stays that way -- repair.ps1 parses -Json, and quickstart prints "N check(s) failed" from it.
# An indeterminate required check is neither a failure nor a completion, and quickstart needs
# to know about it to stop saying SETUP COMPLETE over one.
try {
    $sumDir = Join-Path $repo ".setup\logs"
    if (-not (Test-Path $sumDir)) { New-Item -ItemType Directory -Force $sumDir | Out-Null }
    Set-Content -Path (Join-Path $sumDir "doctor_summary.txt") -Encoding ASCII -Value @(
        ("bad=" + $script:bad), ("warn=" + $script:warn), ("unknown=" + $script:unknown))
} catch { }

exit $script:bad
