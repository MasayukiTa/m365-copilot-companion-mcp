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
