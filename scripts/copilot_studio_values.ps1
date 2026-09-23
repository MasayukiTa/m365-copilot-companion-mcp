# =============================================================================
#  copilot_studio_values.ps1 -- print the EXACT 3 values to paste into Copilot
#  Studio's MCP connector, read straight from your .env + Dev Tunnel, so STEP 4
#  is copy-the-3-lines instead of guessing URLs/headers. ASCII / ENGLISH ONLY.
# =============================================================================
$ErrorActionPreference = "SilentlyContinue"
# This script lives in <repo>\scripts; the .env it reads is at the REPO ROOT (one level up).
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir

$envv = @{}
$p = Join-Path $repo ".env"
if (Test-Path $p) {
    foreach ($ln in Get-Content $p) {
        if ($ln -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envv[$matches[1]] = $matches[2].Trim() }
    }
}

# A PLACEHOLDER IN A COPY-ME LIST IS WORSE THAN AN ERROR. This block exists so someone can
# paste three values into Copilot Studio, and this line used to hand them
# "<Dev Tunnel not up yet ...>" formatted exactly like the two real values beside it. It gets
# pasted. The value is either real or it is refused by name.
$turl = $envv['MCP_TUNNEL_URL']
$serverUrl = $null
if ($turl) { $serverUrl = ($turl.TrimEnd('/')) + '/mcp' }

if ($envv['MCP_API_KEY']) { $bearer = 'Bearer ' + $envv['MCP_API_KEY'] }
else { $bearer = '<no Bearer yet -- run quickstart.bat first>' }

Write-Host ""
Write-Host "Copilot Studio  ->  your agent  ->  Tools -> Add a tool -> New tool -> Model Context Protocol" -ForegroundColor Cyan
Write-Host "Auth = API key,  Type = Header.  Paste these 3 values (everything else: defaults):" -ForegroundColor Cyan
Write-Host "==================================================================================="
if ($serverUrl) {
    Write-Host "  1) Server URL     :  " -NoNewline; Write-Host $serverUrl -ForegroundColor Green
} else {
    Write-Host "  1) Server URL     :  NOT AVAILABLE YET" -ForegroundColor Yellow
    Write-Host "                       The Dev Tunnel is not up, so there is no URL to copy." -ForegroundColor Yellow
    Write-Host "                       Run start_all.bat, then run this again. Do not paste" -ForegroundColor Yellow
    Write-Host "                       anything here until it shows an https address." -ForegroundColor Yellow
}
Write-Host "  2) Header name    :  " -NoNewline; Write-Host "Authorization" -ForegroundColor Green
Write-Host "  3) API key value  :  " -NoNewline; Write-Host $bearer -ForegroundColor Green -NoNewline; Write-Host "   (paste the WHOLE line incl. the word Bearer)"
Write-Host "==================================================================================="
Write-Host "Then:  Save  ->  Add connection / Test  (the tool list should load:"
Write-Host "       list_my_tools, read_file, ...)  ->  Publish: visibility = JUST ME."
Write-Host ""
Write-Host "For LOCAL_LOOP / Deep Review, append this file to the agent's Instructions:" -ForegroundColor Cyan
Write-Host ("       " + (Join-Path $repo "docs\examples\local_loop_agent_instructions.txt")) -ForegroundColor Green
Write-Host "Keep the agent's existing instructions; append the file, Save, then Publish again."
Write-Host "Finally: open the agent's chat, copy its URL, and paste it into configure_env.bat."
Write-Host "Verify the whole chain any time with:  doctor.bat"
Write-Host ""

# THE UNLOCK PASSWORD, READ BACK THE WAY THE SERVER READS IT. bootstrap.py, start_all.ps1 and
# quickstart all send the operator here to "re-read" the unlock password, and this script
# printed only the URL, the header and the Bearer -- so a password missed on its one display
# (in the middle of pip output), or minted by the automatic repair after a .env was carried
# from another PC, could be recovered by no documented path (D1 in the 2026-09-24 new-PC
# review). It is stored DPAPI-protected for this Windows account, so it is decrypted by
# scripts\repair_unlock.py --current, which calls tools.secret_store.unlock_password_from_env
# -- the function the server's unlock gate itself uses -- rather than a second decryption here
# that could disagree with it. Read-only: nothing is written.
function Get-UnlockPasswordLine([string]$repoDir, [string]$envFile) {
    $py = Join-Path $repoDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { $cmd = Get-Command py -ErrorAction SilentlyContinue }
        if ($cmd) { $py = $cmd.Source } else { return "failed:no Python found (.venv is missing -- run quickstart.bat first)" }
    }
    $helper = Join-Path $repoDir "scripts\repair_unlock.py"
    if (-not (Test-Path $helper)) { return "failed:scripts\repair_unlock.py is missing from this checkout" }
    # STDOUT ONLY. secret_store logs a DPAPI failure on stderr, and 2>&1 would put that line
    # where the verdict is looked for. Picked BY PREFIX, as start_all.ps1 does.
    $lines = @(& $py $helper --current $envFile 2>$null)
    $line = $lines | Where-Object { $_ -match '^(password|unset|undecryptable|failed):' } | Select-Object -Last 1
    if (-not $line) { return "failed:scripts\repair_unlock.py --current printed no verdict" }
    return [string]$line
}

Write-Host "For mutating tools (write_file, run_python, shell) you also need, in chat:" -ForegroundColor Cyan
if (-not (Test-Path $p)) {
    Write-Host "  Unlock password  :  NOT AVAILABLE -- there is no .env yet. Run quickstart.bat first." -ForegroundColor Yellow
} else {
    $u = Get-UnlockPasswordLine $repo $p
    if ($u -like "password:*") {
        Write-Host "  Unlock password  :  " -NoNewline; Write-Host $u.Substring("password:".Length) -ForegroundColor Green
        Write-Host "                      (type it as unlock(<password>) in the agent chat; keep it secret)"
    } elseif ($u -like "undecryptable:*") {
        Write-Host "  Unlock password  :  CANNOT BE READ ON THIS PC" -ForegroundColor Yellow
        Write-Host "                      .env holds a value this Windows account cannot decrypt (it was made by" -ForegroundColor Yellow
        Write-Host "                      another account or on another PC). start_all.bat replaces it with a new" -ForegroundColor Yellow
        Write-Host "                      one automatically; to do it now, run:" -ForegroundColor Yellow
        Write-Host "                        .venv\Scripts\python.exe scripts\repair_unlock.py --show" -ForegroundColor Yellow
        Write-Host "                      then run this again to see it." -ForegroundColor Yellow
    } elseif ($u -like "unset:*") {
        Write-Host "  Unlock password  :  NOT SET YET -- run quickstart.bat (STEP 1 creates it)." -ForegroundColor Yellow
    } else {
        Write-Host ("  Unlock password  :  could not be read: " + $u.Substring($u.IndexOf(':') + 1)) -ForegroundColor Yellow
        Write-Host "                      Fix that, then run this again. rotate_secrets.bat --unlock makes a new one." -ForegroundColor Yellow
    }
}
Write-Host ""
