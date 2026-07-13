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
# This script lives in <repo>\scripts; the .env it records the tunnel name/URL into is at the
# REPO ROOT (one level up).
$root = Split-Path -Parent $PSScriptRoot
$DEFAULT_NAME = "m365-copilot-companion"

# Anonymous tunnel access is an EXPLICIT opt-in (MCP_TUNNEL_ALLOW_ANONYMOUS), default OFF.
# Granting --allow-anonymous / --anonymous makes this server (file/shell tools on the tunnel's
# port) reachable by ANYONE on the internet, gated only by the app-layer MCP_API_KEY -- that must
# be a deliberate choice by the operator, not a silent default. Checked as a real environment
# variable first, falling back to the value recorded in .env. Accepts "1"/"true"/"yes"
# case-insensitively; anything else -- including unset -- is OFF.
function Get-AllowAnonymous {
    $v = $env:MCP_TUNNEL_ALLOW_ANONYMOUS
    if (-not $v) {
        $envPath0 = Join-Path $root ".env"
        if (Test-Path $envPath0) {
            foreach ($ln in Get-Content $envPath0) {
                if ($ln -match '^MCP_TUNNEL_ALLOW_ANONYMOUS=(.*)$') { $v = $matches[1]; break }
            }
        }
    }
    if (-not $v) { return $false }
    return ($v.Trim().ToLowerInvariant() -in @("1", "true", "yes"))
}
$AllowAnonymous = Get-AllowAnonymous

# Run devtunnel and return stdout lines with the welcome/banner/upgrade noise stripped.
function Dt {
    $out = & devtunnel @args 2>&1 | Out-String
    return ($out -split "`r?`n" | Where-Object {
        $_ -and ($_ -notmatch 'Welcome to dev tunnels|License Terms|Privacy Statement|Report issues on|devtunnel --help|older version|upgrade to the latest|using one of|Direct download:|Package manager|^\s*$') })
}

# Derive a short, stable-per-machine suffix so a tunnel-id collision (devtunnel ids live in the
# GLOBAL devtunnels.ms namespace, so two different users' clones of this repo both trying to
# create the same default name WILL collide) can be resolved with a name that is unique per
# machine/user yet stable across re-runs on that same machine (the URL must not keep changing --
# Copilot Studio is registered against it). Lowercase alnum only, valid for a devtunnel id.
function Get-MachineSuffix {
    $seed = "$env:COMPUTERNAME|$env:USERNAME"
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $bytes = $sha1.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($seed))
    } finally {
        $sha1.Dispose()
    }
    $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    return $hex.Substring(0, 6)
}

# Privacy guard: some tunnel names leak an identifying (organization/user) token to the
# GLOBAL devtunnels.ms namespace, which is visible to Microsoft and to the tunnel owner.
# These two SHA-256 values are a blocklist of the specific leaked token and the specific
# leaked full tunnel name seen in the wild -- the plaintext is intentionally never written
# here; only its hash is, so this file cannot itself leak it. Hashing is over the UTF-8
# bytes of the lowercased input, hex-encoded lowercase.
$script:TOKEN_SHA256 = "2a0341296bb96dc7d205036f9f693427809772f6136a46f58b04a1c492de9e04"  # gitleaks:allow
$script:FULLNAME_SHA256 = "5ba174b8e87faf4e8106e36a7cf5a901bbec3435d01fbd56914c2b0346858261"  # gitleaks:allow

function Get-Sha256Hex([string]$s) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))
    } finally {
        $sha256.Dispose()
    }
    return (-join ($bytes | ForEach-Object { $_.ToString("x2") }))
}

# Detects whether a candidate dev tunnel name leaks an identifying token. Returns $true if
# the name (or one of its hyphen/underscore/dot/space-separated tokens) is identifying,
# $false otherwise (including an empty/whitespace name -- nothing to leak, "no name set").
function Test-IdentifyingTunnelName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $false }
    $lower = $name.ToLowerInvariant()

    # 1. Whole-name blocklist hash match.
    if ((Get-Sha256Hex $lower) -eq $script:FULLNAME_SHA256) { return $true }

    # 2. Per-token blocklist hash match.
    $tokens = @($lower -split '[^a-z0-9]+' | Where-Object { $_ })
    foreach ($t in $tokens) {
        if ((Get-Sha256Hex $t) -eq $script:TOKEN_SHA256) { return $true }
    }

    # 3. Generic runtime checks (no hash needed) -- catches folder-derived / user-derived
    #    names on any machine, beyond the specific blocklist above.
    $repoLeaf = (Split-Path -Leaf $root).ToLowerInvariant()
    $userName = ("$env:USERNAME").ToLowerInvariant()
    foreach ($t in $tokens) {
        if (($repoLeaf -and $t -eq $repoLeaf) -or ($userName -and $t -eq $userName)) { return $true }
    }
    if (($repoLeaf -and $lower.Contains($repoLeaf)) -or ($userName -and $lower.Contains($userName))) { return $true }

    return $false
}

# Non-fatal notes collected from tolerated (idempotent "already exists") non-zero devtunnel exits,
# surfaced later only if the tunnel ultimately fails to come up -- so the real CLI text is visible
# instead of silently swallowed.
$script:DtWarnings = @()

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
        $dlUri = "https://aka.ms/TunnelsCliDownload/win-x64"
        $payload = Join-Path $dir "devtunnel.download"

        # Download to a temporary payload file; we validate the bytes before trusting it as the exe.
        # Some corporate networks do TLS interception which breaks certificate trust; on such an error
        # we retry once through the system default proxy with the user's default credentials.
        $downloaded = $false
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $dlUri -OutFile $payload -TimeoutSec 120
            $downloaded = $true
        } catch {
            $msg = "$($_.Exception.Message) $($_.Exception.InnerException.Message)"
            if ($msg -match 'certificate|trust relationship|SSL|TLS') {
                Write-Host "      first download attempt hit a certificate/TLS error -> retrying via the system proxy..."
                try {
                    $proxyUri = [System.Net.WebRequest]::DefaultWebProxy.GetProxy([Uri]$dlUri)
                    if ($proxyUri -and ($proxyUri.AbsoluteUri -ne ([Uri]$dlUri).AbsoluteUri)) {
                        Invoke-WebRequest -UseBasicParsing -Uri $dlUri -OutFile $payload -TimeoutSec 120 -Proxy $proxyUri.AbsoluteUri -ProxyUseDefaultCredentials
                    } else {
                        Invoke-WebRequest -UseBasicParsing -Uri $dlUri -OutFile $payload -TimeoutSec 120 -ProxyUseDefaultCredentials
                    }
                    $downloaded = $true
                } catch {
                    $downloaded = $false
                }
            }
        }
        if (-not $downloaded) {
            Write-Host "      ERROR: could not download devtunnel."
            Write-Host "      Your company network blocked this download. Ask IT (or use another PC) to download"
            Write-Host "      devtunnel for Windows x64, save it as %LOCALAPPDATA%\devtunnel\devtunnel.exe, then run quickstart.bat again."
            exit 1
        }

        # aka.ms/TunnelsCliDownload may serve a ZIP rather than a bare .exe. Inspect the first bytes:
        # a ZIP begins with the signature PK\x03\x04 (0x50 0x4B 0x03 0x04).
        $isZip = $false
        try {
            $fs = [System.IO.File]::OpenRead($payload)
            try {
                $sig = New-Object byte[] 4
                $n = $fs.Read($sig, 0, 4)
            } finally { $fs.Close() }
            if ($n -ge 4 -and $sig[0] -eq 0x50 -and $sig[1] -eq 0x4B -and $sig[2] -eq 0x03 -and $sig[3] -eq 0x04) { $isZip = $true }
        } catch { }

        if ($isZip) {
            Write-Host "      download is a .zip -> extracting devtunnel.exe..."
            $zip = Join-Path $dir "devtunnel.zip"
            if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
            Rename-Item -LiteralPath $payload -NewName "devtunnel.zip" -Force
            try {
                Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
            } catch {
                Write-Host "      ERROR: could not extract the downloaded archive: $($_.Exception.Message)"
                Write-Host "      Ask IT (or use another PC) to download devtunnel for Windows x64, save it as"
                Write-Host "      %LOCALAPPDATA%\devtunnel\devtunnel.exe, then run quickstart.bat again."
                exit 1
            }
            if (-not (Test-Path $exe)) {
                $found = Get-ChildItem -LiteralPath $dir -Filter "devtunnel.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($found) { Copy-Item -LiteralPath $found.FullName -Destination $exe -Force }
            }
        } else {
            if (Test-Path $exe) { Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue }
            Rename-Item -LiteralPath $payload -NewName "devtunnel.exe" -Force
        }

        if (-not (Test-Path $exe)) {
            Write-Host "      ERROR: devtunnel.exe was not found after download."
            Write-Host "      Ask IT (or use another PC) to download devtunnel for Windows x64, save it as"
            Write-Host "      %LOCALAPPDATA%\devtunnel\devtunnel.exe, then run quickstart.bat again."
            exit 1
        }

        $env:Path = "$dir;$env:Path"
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$dir*") { [Environment]::SetEnvironmentVariable("Path", "$dir;$userPath", "User") }
        Write-Host "      installed devtunnel.exe -> $dir  (added to your PATH; new terminals pick it up)"

        # Verify the binary actually runs before we go anywhere near sign-in.
        $verOk = $false
        try {
            $ver = (& $exe --version 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $ver) { $verOk = $true }
        } catch { $verOk = $false }
        if (-not $verOk) {
            Write-Host "      ERROR: the downloaded devtunnel.exe did not run ('$exe --version' failed)."
            Write-Host "      Delete %LOCALAPPDATA%\devtunnel\devtunnel.exe and run quickstart.bat again; if it still fails,"
            Write-Host "      ask IT (or use another PC) to download devtunnel for Windows x64 to that same path."
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
        # User forced device-code directly: this prints the URL + code and blocks until done.
        devtunnel user login -d
    } else {
        # Launch the browser login WITHOUT blocking, then poll for up to 120s so a legitimate MFA
        # browser sign-in has time to complete. We never start the device-code flow while this one
        # may still be running -- only after this window closes without success.
        Start-Process devtunnel -ArgumentList @("user", "login") -WindowStyle Hidden | Out-Null
        $signedIn = $false
        for ($i = 0; $i -lt 24; $i++) {
            if (((Dt user show) -join " ") -match 'Logged in as') { $signedIn = $true; break }
            $remaining = 120 - ($i * 5)
            Write-Host "      Waiting for you to finish the Microsoft sign-in in the browser... ${remaining}s"
            Start-Sleep -Seconds 5
        }
        if (-not $signedIn -and ((Dt user show) -join " ") -match 'Logged in as') { $signedIn = $true }
        if (-not $signedIn) {
            Write-Host "      The browser sign-in did not complete in time -> DEVICE CODE."
            Write-Host "      A URL and a short code will be shown below. Open https://microsoft.com/devicelogin and enter the code:"
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
# All tunnel IDs found in the listing, as "name.cluster".
$existingIds = @($listed | Select-String -Pattern '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s' -AllMatches |
    ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value })
# Bare tunnel names (strip the .cluster suffix).
$existingNames = @($existingIds | ForEach-Object { ($_ -split '\.')[0] })

# A previous run (or the supervisor) may have recorded the tunnel name in .env.
# That name MUST win over the default: creating a differently-named tunnel here
# would rewrite .env and silently break an already-configured Copilot Studio
# connector pointing at the old URL -- UNLESS that recorded (or explicitly passed)
# name is itself identifying, in which case privacy wins (see the guard below).
if (-not $TunnelName) {
    $envPath0 = Join-Path $root ".env"
    if (Test-Path $envPath0) {
        foreach ($ln in Get-Content $envPath0) {
            if ($ln -match '^MCP_TUNNEL_NAME=(.+)$') { $TunnelName = $matches[1].Trim(); break }
        }
    }
}

# The default is made globally unique (and stable per machine) FROM THE FIRST RUN by
# appending the machine suffix -- this prevents two different clones/users both trying
# to create the same generic default name from colliding in the global devtunnels.ms
# namespace, without identifying either of them. The plain (unsuffixed) default is still
# recognized below for backward compatibility with installs created before this change.
$machineSuffix = Get-MachineSuffix
$safeDefault = "$DEFAULT_NAME-$machineSuffix"

# Privacy guard: an explicitly-passed -TunnelName or a name recorded in .env is normally
# honored as-is (see above) -- UNLESS it is identifying, in which case it must NOT be
# reused/adopted. Discard it and fall through to the fresh, safe, machine-unique default
# instead, and tell the user the public URL changed and how to clean up the old tunnel.
if ($TunnelName -and (Test-IdentifyingTunnelName $TunnelName)) {
    Write-Host ""
    Write-Host "NOTICE: the previous Dev Tunnel name ('$TunnelName') is identifying (it leaks" -ForegroundColor Yellow
    Write-Host "        an organization/user token to the dev tunnel service) and has been" -ForegroundColor Yellow
    Write-Host "        replaced with a private name for this machine." -ForegroundColor Yellow
    Write-Host "        The PUBLIC URL will change -- after this run, re-paste the new" -ForegroundColor Yellow
    Write-Host "        MCP_TUNNEL_URL into the Copilot Studio MCP connector." -ForegroundColor Yellow
    Write-Host "        Manual cleanup of the old tunnel (optional; not done automatically):" -ForegroundColor Yellow
    Write-Host "            devtunnel delete $TunnelName" -ForegroundColor Yellow
    Write-Host ""
    $TunnelName = ""
}

$reuse = $false
if ($TunnelName) {
    $target = $TunnelName
    if ($existingNames -contains $TunnelName) { $reuse = $true }
} elseif ($existingNames -contains $safeDefault) {
    # Prefer the tunnel matching our (suffixed) default name; never adopt an arbitrary
    # first tunnel.
    $target = $safeDefault
    $reuse = $true
} elseif ($existingNames -contains $DEFAULT_NAME) {
    # Backward-compat: a run from before this change may have created the bare,
    # unsuffixed default name. That name is itself safe/generic (not identifying) --
    # reuse it rather than creating a second tunnel.
    $target = $DEFAULT_NAME
    $reuse = $true
} else {
    $target = $safeDefault
}

if ($reuse) {
    Write-Host "[3/4] reusing existing tunnel: $target"
} elseif (-not ($existingNames -contains $target)) {
    if ($AllowAnonymous) {
        Write-Host "[3/4] creating tunnel '$target' (anonymous-reachable, so Copilot Studio can connect)..."
        $createOut = Dt create $target --allow-anonymous
    } else {
        Write-Host "[3/4] creating tunnel '$target' (NOT anonymous-reachable; MCP_TUNNEL_ALLOW_ANONYMOUS is not set to 1)..."
        $createOut = Dt create $target
    }
    $createExit = $LASTEXITCODE
    if ($createExit -ne 0) {
        $createMsg = ($createOut -join "`n")
        Write-Host "      devtunnel create failed (exit ${createExit}):"
        Write-Host "      $createMsg"
        if ($createMsg -match 'already exists|already in use|conflict|taken|forbidden|permission') {
            $suffix = Get-MachineSuffix
            $newTarget = "$target-$suffix"
            Write-Host "      name collision in the global devtunnels.ms namespace -> retrying ONCE with a machine-unique name: $newTarget"
            if ($AllowAnonymous) {
                $createOut2 = Dt create $newTarget --allow-anonymous
            } else {
                $createOut2 = Dt create $newTarget
            }
            $createExit2 = $LASTEXITCODE
            if ($createExit2 -ne 0) {
                $createMsg2 = ($createOut2 -join "`n")
                Write-Host "      ERROR: devtunnel create failed again (exit ${createExit2}):"
                Write-Host "      $createMsg2"
                Write-Host "      devtunnel could not create/host the tunnel; the error above is from the devtunnel CLI."
                Write-Host "      If it says the name is taken, this is now auto-suffixed per machine; re-run once."
                Write-Host "      If login/permission, run: devtunnel user login"
                exit 1
            }
            # Success on the suffixed retry -- this becomes the name we host/record from now on,
            # and it will be picked up as MCP_TUNNEL_NAME on future re-runs so the URL stays stable.
            $target = $newTarget
        } else {
            Write-Host "      devtunnel could not create/host the tunnel; the error above is from the devtunnel CLI."
            Write-Host "      If login/permission, run: devtunnel user login"
            exit 1
        }
    }
} else {
    Write-Host "[3/4] tunnel '$target' exists"
}

if (-not ((Dt port list $target) -match ("\b" + [string]$Port + "\b"))) {
    Write-Host "      adding port $Port..."
    $portOut = Dt port create $target -p $Port --protocol http
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) {
        $portMsg = ($portOut -join "`n")
        if ($portMsg -match 'already exists|already in use|conflict') {
            $script:DtWarnings += "port create ($target, port $Port): $portMsg"
        } else {
            Write-Host "      ERROR: devtunnel port create failed (exit ${portExit}):"
            Write-Host "      $portMsg"
            Write-Host "      devtunnel could not create/host the tunnel; the error above is from the devtunnel CLI."
            Write-Host "      If login/permission, run: devtunnel user login"
            exit 1
        }
    }
}

# Idempotently re-assert anonymous access so a half-configured tunnel from an aborted earlier run
# (created but access never applied, or port added without access) gets repaired on re-run. An
# "already exists"/"conflict" result is treated as success (idempotent); anything else is a real
# error and is surfaced instead of silently swallowed. ONLY done if MCP_TUNNEL_ALLOW_ANONYMOUS
# opted in -- otherwise this tunnel stays closed to the anonymous internet, by design.
if ($AllowAnonymous) {
    $accessOut1 = Dt access create $target --anonymous
    $accessExit1 = $LASTEXITCODE
    if ($accessExit1 -ne 0) {
        $accessMsg1 = ($accessOut1 -join "`n")
        if ($accessMsg1 -match 'already exists|already in use|conflict') {
            $script:DtWarnings += "access create ($target, tunnel): $accessMsg1"
        } else {
            Write-Host "      ERROR: devtunnel access create (tunnel-level) failed (exit ${accessExit1}):"
            Write-Host "      $accessMsg1"
            Write-Host "      devtunnel could not create/host the tunnel; the error above is from the devtunnel CLI."
            Write-Host "      If login/permission, run: devtunnel user login"
            exit 1
        }
    }
    $accessOut2 = Dt access create $target -p $Port --anonymous
    $accessExit2 = $LASTEXITCODE
    if ($accessExit2 -ne 0) {
        $accessMsg2 = ($accessOut2 -join "`n")
        if ($accessMsg2 -match 'already exists|already in use|conflict') {
            $script:DtWarnings += "access create ($target, port $Port): $accessMsg2"
        } else {
            Write-Host "      ERROR: devtunnel access create (port-level) failed (exit ${accessExit2}):"
            Write-Host "      $accessMsg2"
            Write-Host "      devtunnel could not create/host the tunnel; the error above is from the devtunnel CLI."
            Write-Host "      If login/permission, run: devtunnel user login"
            exit 1
        }
    }
} else {
    Write-Host "      MCP_TUNNEL_ALLOW_ANONYMOUS is not set to 1 -- skipping anonymous access grant."
    Write-Host "      This tunnel is NOT anonymously reachable. A remote client (e.g. Copilot Studio)"
    Write-Host "      will NOT be able to connect until you either:"
    Write-Host "        (a) set MCP_TUNNEL_ALLOW_ANONYMOUS=1 and re-run this script (accepts exposing"
    Write-Host "            the server to the anonymous internet, gated only by the MCP_API_KEY"
    Write-Host "            app-layer key), or"
    Write-Host "        (b) grant Entra/tenant-scoped access instead, e.g.:"
    Write-Host "            devtunnel access create $target --tenant <your-tenant-id>"
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
    if ($script:DtWarnings.Count -gt 0) {
        Write-Host " devtunnel CLI messages captured along the way (may explain the failure):"
        foreach ($w in $script:DtWarnings) { Write-Host "   $w" }
        Write-Host ""
    }
    Write-Host " Tunnel '$target' is set up but no public URL appeared after hosting."
    Write-Host "==================================================================="
    Write-Host " devtunnel could not create/host the tunnel; any error text above is from the devtunnel CLI."
    Write-Host " If it said the name is taken, this script now auto-suffixes the name per machine -- re-run once."
    Write-Host " If login/permission related, run: devtunnel user login"
    Write-Host " Otherwise run  devtunnel host $target  in a separate window and look for the"
    Write-Host " https://...devtunnels.ms URL."
    # Still record the tunnel name so a re-run can reuse it, but fail so quickstart.bat knows
    # the tunnel is NOT ready.
    try {
        $envPath = Join-Path $root ".env"
        if (Test-Path $envPath) {
            $lines = @(Get-Content $envPath | Where-Object { $_ -notmatch '^# devtunnel \(auto\)|^MCP_TUNNEL_NAME=|^MCP_TUNNEL_URL=' })
            $lines += "# devtunnel (auto) -- the public URL to register in Copilot Studio; supervisor hosts MCP_TUNNEL_NAME"
            $lines += "MCP_TUNNEL_NAME=$target"
            Set-Content -Path $envPath -Value $lines -Encoding ASCII
        }
    } catch { }
    exit 1
}
Write-Host "==================================================================="

# record name + URL in .env as references (commented; never touches existing secrets)
try {
    $envPath = Join-Path $root ".env"
    if (Test-Path $envPath) {
        $lines = @(Get-Content $envPath | Where-Object { $_ -notmatch '^# devtunnel \(auto\)|^MCP_TUNNEL_NAME=|^MCP_TUNNEL_URL=' })
        $lines += "# devtunnel (auto) -- the public URL to register in Copilot Studio; supervisor hosts MCP_TUNNEL_NAME"
        $lines += "MCP_TUNNEL_NAME=$target"
        $lines += "MCP_TUNNEL_URL=$url"
        Set-Content -Path $envPath -Value $lines -Encoding ASCII
        Write-Host "Recorded MCP_TUNNEL_NAME and MCP_TUNNEL_URL in .env."
    } else {
        Write-Host "ERROR: .env not found at $envPath -- cannot record MCP_TUNNEL_URL."
        exit 1
    }
} catch {
    Write-Host "ERROR: failed to record MCP_TUNNEL_URL in .env: $($_.Exception.Message)"
    exit 1
}
exit 0
