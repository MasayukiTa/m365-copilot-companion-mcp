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

function Get-LocalSecret([hashtable]$envMap, [string]$plainName, [string]$protectedName) {
    $plain = [string]$envMap[$plainName]
    if (-not [string]::IsNullOrWhiteSpace($plain)) { return @{ State = 'value'; Value = $plain } }
    $protected = [string]$envMap[$protectedName]
    if ([string]::IsNullOrWhiteSpace($protected)) { return @{ State = 'unset'; Value = $null } }
    if (-not $protected.StartsWith('dpapi:', [System.StringComparison]::OrdinalIgnoreCase)) {
        return @{ State = 'undecryptable'; Value = $null }
    }
    try {
        Add-Type -AssemblyName System.Security -ErrorAction SilentlyContinue
        $cipher = [Convert]::FromBase64String($protected.Substring('dpapi:'.Length))
        $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $cipher, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $value = [Text.Encoding]::UTF8.GetString($plainBytes)
        if ([string]::IsNullOrWhiteSpace($value)) { return @{ State = 'undecryptable'; Value = $null } }
        return @{ State = 'value'; Value = $value }
    } catch {
        return @{ State = 'undecryptable'; Value = $null }
    }
}

$a = Get-LocalSecret $envv 'MCP_API_KEY' 'MCP_API_KEY_PROTECTED'
if ($a.State -eq 'value') { $bearer = 'Bearer ' + $a.Value }
elseif ($a.State -eq 'undecryptable') { $bearer = '<Bearer exists but cannot be decrypted on this Windows account>' }
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

# THE UNLOCK PASSWORD IS DISPLAYED ONLY HERE, in the interactive PowerShell process.
# repair_unlock.py is also called by hidden startup with stdout captured into logs, so it no
# longer has any cleartext-output mode. Read the same .env keys here: legacy plain first, then
# the user-bound DPAPI blob produced by tools.secret_store.protect_secret.
function Get-UnlockPasswordLocal([hashtable]$envMap) {
    $r = Get-LocalSecret $envMap 'MCP_UNLOCK_PASSWORD' 'MCP_UNLOCK_PASSWORD_PROTECTED'
    if ($r.State -eq 'value') { return @{ State = 'password'; Value = $r.Value } }
    return @{ State = $r.State; Value = $null }
}

Write-Host "For mutating tools (write_file, run_python, shell) you also need, in chat:" -ForegroundColor Cyan
if (-not (Test-Path $p)) {
    Write-Host "  Unlock password  :  NOT AVAILABLE -- there is no .env yet. Run quickstart.bat first." -ForegroundColor Yellow
} else {
    $u = Get-UnlockPasswordLocal $envv
    if ($u.State -eq "password") {
        Write-Host "  Unlock password  :  " -NoNewline; Write-Host $u.Value -ForegroundColor Green
        Write-Host "                      (type it as unlock(<password>) in the agent chat; keep it secret)"
    } elseif ($u.State -eq "undecryptable") {
        Write-Host "  Unlock password  :  CANNOT BE READ ON THIS PC" -ForegroundColor Yellow
        Write-Host "                      .env holds a value this Windows account cannot decrypt (it was made by" -ForegroundColor Yellow
        Write-Host "                      another account or on another PC). start_all.bat replaces it with a new" -ForegroundColor Yellow
        Write-Host "                      one automatically; to do it now, run:" -ForegroundColor Yellow
        Write-Host "                        start_all.bat" -ForegroundColor Yellow
        Write-Host "                      then run this again to see it." -ForegroundColor Yellow
    } elseif ($u.State -eq "unset") {
        Write-Host "  Unlock password  :  NOT SET YET -- run quickstart.bat (STEP 1 creates it)." -ForegroundColor Yellow
    } else {
        Write-Host "  Unlock password  :  could not be read." -ForegroundColor Yellow
        Write-Host "                      Fix that, then run this again. rotate_secrets.bat --unlock makes a new one." -ForegroundColor Yellow
    }
}
Write-Host ""
