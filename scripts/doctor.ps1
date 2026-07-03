# =============================================================================
#  doctor.ps1 -- one-glance health check for the m365-copilot-companion-mcp stack.
#  Verifies every link (secrets -> server -> tunnel -> Edge -> M365 sign-in ->
#  auth) and prints a GREEN/RED checklist with the exact fix for each RED line,
#  so "is it actually working?" is answered in one run. Read-only; safe any time.
#  ASCII / ENGLISH ONLY (cmd/console safe).
# =============================================================================
$ErrorActionPreference = "SilentlyContinue"
# This script lives in <repo>\scripts; the .env it reads is at the REPO ROOT (one level up).
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir

# --- load .env into a hashtable -------------------------------------------------
$envv = @{}
$envPath = Join-Path $repo ".env"
if (Test-Path $envPath) {
    foreach ($ln in Get-Content $envPath) {
        if ($ln -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envv[$matches[1]] = $matches[2].Trim() }
    }
}

$script:ok = 0; $script:bad = 0
function Check([string]$name, [scriptblock]$test, [string]$fix) {
    $pass = $false
    try { $pass = [bool](& $test) } catch { $pass = $false }
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
Check ".env present with a Bearer token (MCP_API_KEY)" `
    { $envv.ContainsKey('MCP_API_KEY') -and $envv['MCP_API_KEY'] } `
    "run quickstart.bat -- it creates .env with a fresh Bearer + unlock password"

Check "Agent URL configured (Copilot Studio agent pasted)" `
    { ($envv['MCP_FLEET_AGENT_URL']) -or ($envv['MCP_IMPL_AGENT_URL']) } `
    "double-click configure_env.bat and paste the Copilot Studio agent URL (README STEP 4)"

# 2. local MCP server
Check "MCP server up (http://127.0.0.1:8000/health)" `
    { (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } `
    "start the stack: double-click start_all.bat   (or run .\scripts\start.ps1)"

# 3. Dev Tunnel (public reachability of the server through the tunnel)
$turl = $envv['MCP_TUNNEL_URL']
Check "Dev Tunnel reachable (public URL -> server)" `
    { if (-not $turl) { return $false }; (Invoke-WebRequest -Uri (($turl.TrimEnd('/')) + '/health') -TimeoutSec 7 -UseBasicParsing).StatusCode -eq 200 } `
    "the tunnel host is not serving: run start_all.bat (supervisor hosts it). To (re)create the tunnel: powershell -File scripts\setup_devtunnel.ps1"

# 4. Companion Edge (:9222) for the fleet/agent
Check "Companion Edge running (:9222 fleet/agent)" `
    { Get-Json 'http://127.0.0.1:9222/json/version' | Out-Null; $true } `
    "launch it: powershell -File scripts\start_companion_edge.ps1   (then sign into M365 once)"

Check "M365 signed in on the companion Edge (no login page)" `
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
Check "Bridge Edge running (:9223 history/scrape) [optional]" `
    { Get-Json 'http://127.0.0.1:9223/json/version' | Out-Null; $true } `
    "optional: powershell -File scripts\start_bridge.ps1 -Keepalive   (only needed for past-conversation history)"

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
Check "Auth OK end-to-end (Bearer accepted on /mcp)" `
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
