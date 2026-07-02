# =============================================================================
#  copilot_studio_values.ps1 -- print the EXACT 3 values to paste into Copilot
#  Studio's MCP connector, read straight from your .env + Dev Tunnel, so STEP 4
#  is copy-the-3-lines instead of guessing URLs/headers. ASCII / ENGLISH ONLY.
# =============================================================================
$ErrorActionPreference = "SilentlyContinue"
$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }

$envv = @{}
$p = Join-Path $repo ".env"
if (Test-Path $p) {
    foreach ($ln in Get-Content $p) {
        if ($ln -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envv[$matches[1]] = $matches[2].Trim() }
    }
}

$turl = $envv['MCP_TUNNEL_URL']
if ($turl) { $serverUrl = ($turl.TrimEnd('/')) + '/mcp' }
else { $serverUrl = '<Dev Tunnel not up yet -- run start_all.bat (or setup_devtunnel.ps1), then re-run this>' }

if ($envv['MCP_API_KEY']) { $bearer = 'Bearer ' + $envv['MCP_API_KEY'] }
else { $bearer = '<no Bearer yet -- run quickstart.bat first>' }

Write-Host ""
Write-Host "Copilot Studio  ->  your agent  ->  Tools -> Add a tool -> New tool -> Model Context Protocol" -ForegroundColor Cyan
Write-Host "Auth = API key,  Type = Header.  Paste these 3 values (everything else: defaults):" -ForegroundColor Cyan
Write-Host "==================================================================================="
Write-Host "  1) Server URL     :  " -NoNewline; Write-Host $serverUrl -ForegroundColor Green
Write-Host "  2) Header name    :  " -NoNewline; Write-Host "Authorization" -ForegroundColor Green
Write-Host "  3) API key value  :  " -NoNewline; Write-Host $bearer -ForegroundColor Green -NoNewline; Write-Host "   (paste the WHOLE line incl. the word Bearer)"
Write-Host "==================================================================================="
Write-Host "Then:  Save  ->  Add connection / Test  (the tool list should load:"
Write-Host "       list_my_tools, read_file, ...)  ->  Publish: visibility = JUST ME."
Write-Host "Finally: open the agent's chat, copy its URL, and paste it into configure_env.bat."
Write-Host "Verify the whole chain any time with:  doctor.bat"
Write-Host ""
