# =============================================================================
#  doctor.ps1 -- one-glance health check for the m365-copilot-companion-mcp stack.
#  Verifies every link (secrets -> server -> tunnel -> Edge -> M365 sign-in ->
#  auth) and prints a GREEN/RED checklist with the exact fix for each RED line,
#  so "is it actually working?" is answered in one run. Read-only; safe any time.
#  ASCII / ENGLISH ONLY (cmd/console safe).
#
#  -Json: emit ONE compressed JSON array line on stdout (id/ok/name/fix/optional/
#  info/skipped per check) instead of the colored checklist. This is the single
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

$script:ok = 0; $script:bad = 0
# Machine-readable accumulator: one entry per check, in the exact order checks run
# (== the dependency order documented at the top of this file). This -- not the
# colored console text -- is the single source of truth a repair dispatcher reads.
$script:results = @()
function Add-Result([string]$id, [bool]$pass, [string]$name, [string]$fix, [bool]$optional, [bool]$info, [bool]$skipped) {
    $script:results += [PSCustomObject]@{
        id       = $id
        ok       = $pass
        name     = $name
        fix      = $fix
        optional = $optional
        info     = $info
        skipped  = $skipped
    }
}
function Check([string]$id, [string]$name, [scriptblock]$test, [string]$fix, [switch]$Optional, [switch]$Info) {
    $pass = $false
    try { $pass = [bool](& $test) } catch { $pass = $false }
    Add-Result $id $pass $name $fix $Optional.IsPresent $Info.IsPresent $false
    if ($pass) {
        Write-Host ("  [ OK ] " + $name) -ForegroundColor Green
        $script:ok++
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

# 2. local MCP server
Check "server_up" "MCP server up (http://127.0.0.1:8000/health)" `
    { (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } `
    "start the stack: double-click start_all.bat   (or run .\scripts\start.ps1)"

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
# error). Uses Check (not TunnelCheck) so it runs independently of the tunnel
# dependency short-circuit above -- an unowned name is informative even when e.g.
# the CLI isn't logged in (in which case the bounded call below just no-ops to a
# safe PASS, since there is nothing to contradict "empty or unknown"). Bounded the
# same way as the other devtunnel calls in this file.
function Test-TunnelOwned([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $true }
    $out = Invoke-DevTunnelBounded @('list') 8
    if (-not $out) { return $false }
    $bareName = (($name -split '\.')[0]).ToLowerInvariant()
    $ids = @($out -split "`r?`n" | ForEach-Object {
        if ($_ -match '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s') { $matches[1] }
    } | Where-Object { $_ } | ForEach-Object { (($_ -split '\.')[0]).ToLowerInvariant() })
    return ($ids -contains $bareName)
}

Check "tunnel_owned" "Dev Tunnel name is owned by this account (MCP_TUNNEL_NAME)" `
    { Test-TunnelOwned $tname } `
    "This .env names a dev tunnel your account does not own (it was likely copied from another machine). Run start_all.bat (it now repoints to your own tunnel automatically) or: powershell -File scripts\heal_tunnel.ps1"

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
function Get-RunningSupervisorCommandLineDoctor {
    try {
        $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and ($_.CommandLine -match 'supervisor\.ps1') } |
             Select-Object -First 1
        if ($p) { return $p.CommandLine }
    } catch { }
    return ""
}
Check "tunnel_supervisor_match" "Running supervisor hosts the tunnel named in .env" `
    {
        $runCmdLine = Get-RunningSupervisorCommandLineDoctor
        if (-not $runCmdLine) { return $true }   # no supervisor running -- nothing to mismatch
        -not (Test-SupervisorTunnelDrift -RunningCommandLine $runCmdLine -EnvTunnelName $tname)
    } `
    "The running supervisor is hosting a different (stale/borrowed) tunnel than .env names. Re-run start_all.bat -- it now stops the stale supervisor and re-hosts your own tunnel."

# 4. Companion Edge (:9222) for the fleet/agent
Check "edge_companion" "Companion Edge running (:9222 fleet/agent)" `
    { Get-Json 'http://127.0.0.1:9222/json/version' | Out-Null; $true } `
    "launch it: powershell -File scripts\start_companion_edge.ps1   (then sign into M365 once)"

Check "m365_signin" "M365 signed in on the companion Edge (no login page)" `
    {
        $tabs = Get-Json 'http://127.0.0.1:9222/json'
        # A login WALL on ANY tab means sign-in is still required -- even if a separate
        # chat tab is also open. A corporate ADFS/Entra tab can sit on the login page
        # while another tab shows chat; that must read RED, not green. Broadened pattern:
        # login.microsoftonline / login.live.com / /adfs/ / adfs. / /oauth2/authorize /
        # /signin / login_hint= (mirrors relay/edge_recover.looks_like_login's spirit).
        $loginRe = 'login\.microsoftonline|login\.live\.com|/adfs/|adfs\.|/oauth2/authorize|/signin|login_hint='
        $onLoginWall = @($tabs | Where-Object { $_.url -match $loginRe }).Count -gt 0
        $m = $tabs | Where-Object { $_.url -match 'm365|copilot' }
        ($m) -and -not $onLoginWall
    } `
    "sign-in needed: run  powershell -File scripts\start_companion_edge.ps1 -Foreground  (or python -m relay.edge_recover then surface()) to bring the companion Edge window forward, then complete M365 (Entra ID) sign-in -- it persists across restarts"

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
if ($script:bad -eq 0) {
    Write-Host ("ALL GREEN ({0} checks). You're set." -f $script:ok) -ForegroundColor Green
    Write-Host "Daily startup: double-click the 'M365 Companion' icon on your Desktop." -ForegroundColor Green
} else {
    Write-Host ("{0} OK, {1} need attention -- fix the red lines above, then re-run: doctor.bat" -f $script:ok, $script:bad) -ForegroundColor Yellow
}
Write-Host ""

if ($Json) {
    # Bind directly (not via the pipeline) so ConvertTo-Json always serializes this as
    # ONE JSON array, even in the edge case where $script:results has exactly one entry
    # (piping would unwrap it and emit a bare object instead of a 1-element array).
    Write-Output (ConvertTo-Json -InputObject $script:results -Compress)
}

# Nonzero exit whenever anything needs attention, in BOTH modes -- additive only: no
# existing caller reads this script's exit code (doctor.bat just runs it then `pause`;
# start_all.ps1 does not invoke doctor.ps1 at all), so this cannot break anything that
# already works, and it gives scripts\repair.ps1 (and any other automation) a cheap
# "is there anything to do" signal without re-parsing -Json output.
exit $script:bad
