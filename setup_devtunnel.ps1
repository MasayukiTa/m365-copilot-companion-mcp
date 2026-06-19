# setup_devtunnel.ps1 -- robust, idempotent Dev Tunnel setup for the MCP server's PUBLIC URL.
#
# Fixes the three first-run defects seen on a fresh PC:
#   1. winget did not install devtunnel  -> falls back to the official DIRECT DOWNLOAD (no winget).
#   2. `devtunnel login` browser did not open / Entra ID failed -> falls back to DEVICE-CODE sign-in
#      (works on any machine, no browser popup: shows a code to enter at microsoft.com/devicelogin).
#   3. the Dev Tunnel URL never appeared -> this script CREATES the tunnel + port and PRINTS the
#      public URL (and records it + the tunnel name in .env) so you can paste it into Copilot Studio.
#
# Idempotent: an already-installed CLI / existing sign-in / existing tunnel are reused, not recreated.
#   .\setup_devtunnel.ps1                 # install+login+ensure tunnel, print the URL
#   .\setup_devtunnel.ps1 -DeviceCode     # force device-code sign-in (no browser)
#   .\setup_devtunnel.ps1 -TunnelName foo # use/create a specific tunnel name
param(
    [string]$TunnelName = "",     # empty -> reuse the existing tunnel if there is one, else create a default
    [int]$Port = 8000,
    [switch]$DeviceCode
)
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$DEFAULT_NAME = "m365-copilot-companion"

# Run devtunnel and return stdout lines with the welcome/banner/upgrade noise stripped.
function Dt {
    $out = & devtunnel @args 2>&1 | Out-String
    return ($out -split "`r?`n" | Where-Object {
        $_ -and ($_ -notmatch 'Welcome to dev tunnels|License Terms|Privacy Statement|Report issues on|devtunnel --help|older version|upgrade to the latest|using one of|Direct download:|Package manager|^\s*$') })
}

# --- 1. ensure the CLI is installed ----------------------------------------------------------
if (-not (Get-Command devtunnel -ErrorAction SilentlyContinue)) {
    Write-Host "[1/4] devtunnel CLI not found."
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "      trying: winget install Microsoft.devtunnel ..."
        try {
            winget install --id Microsoft.devtunnel --accept-source-agreements --accept-package-agreements -e --silent | Out-Null
        } catch { }
        $installed = [bool](Get-Command devtunnel -ErrorAction SilentlyContinue)
    }
    if (-not $installed) {
        Write-Host "      winget unavailable/failed -> DIRECT DOWNLOAD (no winget needed)..."
        $dir = Join-Path $env:LOCALAPPDATA "devtunnel"
        New-Item -ItemType Directory -Force $dir | Out-Null
        $exe = Join-Path $dir "devtunnel.exe"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://aka.ms/TunnelsCliDownload/win-x64" -OutFile $exe -TimeoutSec 120
            $env:Path = "$dir;$env:Path"
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            if ($userPath -notlike "*$dir*") { [Environment]::SetEnvironmentVariable("Path", "$dir;$userPath", "User") }
            Write-Host "      installed devtunnel.exe -> $dir  (added to your PATH; new terminals pick it up)"
        } catch {
            Write-Host "      ERROR: direct download failed: $($_.Exception.Message)"
            Write-Host "      Download it by hand from https://aka.ms/TunnelsCliDownload/win-x64 and put devtunnel.exe on PATH."
            exit 1
        }
    }
} else {
    Write-Host "[1/4] devtunnel CLI: already installed"
}
Write-Host ("      version: " + ((Dt --version) -join " "))

# --- 2. ensure signed in ---------------------------------------------------------------------
$who = (Dt user show) -join " "
if ($who -match 'Logged in as') {
    Write-Host ("[2/4] sign-in: already signed in -- " + $who)
} else {
    Write-Host "[2/4] sign-in required. A browser (or a device code) will appear -- complete the Entra ID / Microsoft sign-in."
    if ($DeviceCode) {
        devtunnel user login -d
    } else {
        try { devtunnel user login } catch { }
        Start-Sleep -Seconds 2
        if (((Dt user show) -join " ") -notmatch 'Logged in as') {
            Write-Host "      browser sign-in did not complete -> DEVICE CODE. Open https://microsoft.com/devicelogin and enter the code shown:"
            devtunnel user login -d
        }
    }
    $who = (Dt user show) -join " "
    if ($who -notmatch 'Logged in as') {
        Write-Host "      ERROR: still not signed in. Run  devtunnel user login -d  manually (device code), then re-run this."
        exit 1
    }
    Write-Host ("      signed in -- " + $who)
}

# --- 3. ensure the tunnel + port exist (idempotent; reuse an existing tunnel) -----------------
$listed = Dt list
$existingId = ($listed | Select-String -Pattern '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
if ($TunnelName) {
    $target = $TunnelName
} elseif ($existingId) {
    $target = ($existingId -split '\.')[0]   # strip the .cluster suffix -> the tunnel name
    Write-Host "[3/4] reusing existing tunnel: $existingId"
} else {
    $target = $DEFAULT_NAME
}
if (-not ($listed -match [regex]::Escape($target))) {
    Write-Host "[3/4] creating tunnel '$target' (anonymous-reachable, so Copilot Studio can connect)..."
    Dt create $target --allow-anonymous | Out-Null
} else {
    Write-Host "[3/4] tunnel '$target' exists"
}
if (-not ((Dt port list $target) -match ("\b" + [string]$Port + "\b"))) {
    Write-Host "      adding port $Port..."
    Dt port create $target -p $Port --protocol http | Out-Null
}

# --- 4. host the tunnel so the public URL is assigned, then surface it ------------------------
# IMPORTANT: a freshly-created tunnel has NO port URL in `devtunnel show` until it is HOSTED at
# least once (verified: Host connections must be >= 1 for the https://...devtunnels.ms URL to
# appear). So if the URL isn't there yet, start a host in the background and poll until it shows up,
# THEN write it. (This is the bug that left .env without a URL on a fresh PC.)
function Tunnel-Url($name) {
    $s = Dt show $name
    return ($s | Select-String -Pattern 'https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*' -AllMatches |
            ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } | Select-Object -First 1)
}
$url = Tunnel-Url $target
if (-not $url) {
    Write-Host "[4/4] hosting the tunnel to obtain its public URL (a few seconds)..."
    Start-Process devtunnel -ArgumentList @("host", $target) -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $url = Tunnel-Url $target
        if ($url) { break }
    }
    # The host keeps running; the supervisor (start_all) detects the tunnel is already hosted and
    # leaves it / keeps it alive. That is what actually serves Copilot Studio.
}
Write-Host ""
Write-Host "==================================================================="
if ($url) {
    Write-Host " Dev Tunnel READY. Public URL of the MCP server (port $Port):"
    Write-Host ""
    Write-Host "     $url"
    Write-Host ""
    Write-Host " Paste this URL (plus your MCP endpoint path) into the Copilot"
    Write-Host " Studio MCP connector. The tunnel name is: $target"
} else {
    Write-Host " Tunnel '$target' is set up but no port URL was parsed."
    Write-Host " Run:  devtunnel show $target   and copy the https://...devtunnels.ms URL."
}
Write-Host "==================================================================="

# record name + URL in .env as references (commented; never touches existing secrets)
try {
    $envPath = Join-Path $root ".env"
    if (Test-Path $envPath) {
        $lines = @(Get-Content $envPath | Where-Object { $_ -notmatch '^# devtunnel \(auto\)|^MCP_TUNNEL_NAME=|^MCP_TUNNEL_URL=' })
        $lines += "# devtunnel (auto) -- the public URL to register in Copilot Studio; supervisor hosts MCP_TUNNEL_NAME"
        $lines += "MCP_TUNNEL_NAME=$target"
        if ($url) { $lines += "MCP_TUNNEL_URL=$url" }
        Set-Content -Path $envPath -Value $lines -Encoding ASCII
        Write-Host "Recorded MCP_TUNNEL_NAME (and URL) in .env."
    }
} catch { }
