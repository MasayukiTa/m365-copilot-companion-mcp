# make_second_tunnel.ps1 -- stand up a SECOND Dev Tunnel (a different tunnel id) that exposes
# the SAME local MCP server (127.0.0.1:8000) as the first one, so muse can try two public URLs
# in turn and survive one tunnel/relay dropping out (design B, 2026-11-10).
#
# WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT DO:
#   * Creates (idempotently) a second tunnel whose id is the first tunnel's name + "-r2", adds
#     port 8000 to it, grants it the SAME access the first tunnel has (anonymous / tenant /
#     none -- read back from the first tunnel, never widened), hosts it once to mint its public
#     URL, prints that URL, and records it in .env as MCP_TUNNEL_URL_2 / MCP_TUNNEL_NAME_2
#     (commented reference keys; never touches MCP_API_KEY or any secret value).
#   * It NEVER stops, deletes, re-creates, re-ports, or changes the access of the FIRST tunnel
#     or its running host. Every devtunnel mutation below targets ONLY the second tunnel id.
#     The first tunnel is read (list / show / access list) but never written.
#
# Idempotent: an existing second tunnel / port / access grant is reused, not recreated. Safe to
# re-run; the public URL stays stable because the tunnel id is stable per machine.
#
# Rollback: delete the second tunnel --  devtunnel delete <second-name>  -- which is printed at
# the end. Nothing else is left behind (the MCP_TUNNEL_*_2 lines in .env are references only).
#
#   .\make_second_tunnel.ps1                 # create/reuse the second tunnel, print its URL
#   .\make_second_tunnel.ps1 -Port 8000      # same (8000 is the default)
#   .\make_second_tunnel.ps1 -SecondName foo # use/create a specific second-tunnel id
#
# Exit codes: 0 second tunnel ready and its URL recorded; 1 failed; 3 ready but the first
# tunnel has NO access grant (so neither can a remote client reach the second -- nothing to
# copy); 4 access was chosen but did not read back on the second tunnel.
#
# ASCII ONLY (cmd/console safe). No BOM.
param(
    [string]$SecondName = "",   # empty -> first tunnel's name + "-r2" (or the machine-safe default)
    [int]$Port = 8000
)
$ErrorActionPreference = "Continue"

# This script lives in <repo>\scripts; .env is at the repo ROOT (one level up).
$root = Split-Path -Parent $PSScriptRoot
$DEFAULT_NAME = "m365-copilot-companion"
$SECOND_SUFFIX = "r2"

# Shared PURE helpers: identity (Get-MachineSuffix etc.), the privacy guard
# (Test-IdentifyingTunnelName) and the "another machine hosts it" guard. No top-level side
# effects, so dot-sourcing is safe. Same file setup_devtunnel.ps1 / heal_tunnel.ps1 use.
. (Join-Path $PSScriptRoot "tunnel_name_util.ps1")

# --- devtunnel runner: resolve the one binary, strip banner noise (mirrors setup_devtunnel.ps1).
function Find-DevTunnelCli {
    $c = Get-Command devtunnel -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    if (-not $env:LOCALAPPDATA) { return $null }
    foreach ($cand in @((Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"),
                        (Join-Path $env:LOCALAPPDATA "devtunnel\devtunnel.exe"))) {
        if (Test-Path -LiteralPath $cand) { return $cand }
    }
    return $null
}
$script:DtExe = Find-DevTunnelCli
if (-not $script:DtExe) {
    Write-Host "[1/5] ERROR: devtunnel CLI not found. Run scripts\setup_devtunnel.ps1 first (it installs"
    Write-Host "      the CLI and stands up the FIRST tunnel); this script only ADDS a second one."
    exit 1
}
function Dt {
    $out = & $script:DtExe @args 2>&1 | Out-String
    return ($out -split "`r?`n" | Where-Object {
        $_ -and ($_ -notmatch 'Welcome to dev tunnels|License Terms|Privacy Statement|Report issues on|devtunnel --help|older version|upgrade to the latest|using one of|Direct download:|Package manager|^\s*$') })
}
Write-Host ("[1/5] devtunnel CLI: " + $script:DtExe)

# --- 2. must be signed in. This script never triggers the interactive sign-in (that is
# setup_devtunnel.ps1's job); it only adds a second tunnel once the account is already signed in.
$who = (Dt user show) -join " "
if ($who -notmatch 'Logged in as') {
    Write-Host "[2/5] ERROR: not signed in to devtunnel. Run scripts\setup_devtunnel.ps1 (or"
    Write-Host "      devtunnel user login) first, then re-run this script."
    exit 1
}
Write-Host ("[2/5] sign-in: " + $who)

# --- 3. work out the FIRST tunnel's name (read only) and derive the SECOND name from it. -------
# Reuse the SAME readable-list guard setup_devtunnel.ps1 uses: an empty/garbled list is NOT an
# empty account, and must not cause a create.
function Get-OwnedTunnelNames([string[]]$listOutput, [int]$exitCode) {
    if ($exitCode -ne 0) { return $null }
    $ids = @($listOutput | Select-String -Pattern '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s' -AllMatches |
        ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value })
    if ($ids.Count -gt 0) { return ,@($ids | ForEach-Object { ($_ -split '\.')[0] }) }
    if (($listOutput -join "`n") -match '(?i)no (dev )?tunnels|\b0 tunnels') { return ,@() }
    return $null
}
$listed = Dt list
$listExit = $LASTEXITCODE
$existingNames = Get-OwnedTunnelNames $listed $listExit
if ($null -eq $existingNames) {
    Write-Host "[3/5] ERROR: 'devtunnel list' did not return a readable list of your tunnels (exit $listExit)."
    Write-Host "      What it printed:"
    foreach ($l in @($listed)) { Write-Host ("        " + $l) }
    Write-Host "      Nothing was created: whether the second tunnel already exists is decided from this"
    Write-Host "      list, and guessing would mint a second public URL. Usually a passing network/sign-in"
    Write-Host "      hiccup -- re-run. If it repeats, run 'devtunnel list' yourself."
    exit 1
}

# The FIRST tunnel's name: MCP_TUNNEL_NAME in .env if present (that is the one the supervisor
# hosts), else this machine's generated default. Read only -- we never write this name.
$firstName = ""
$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
    foreach ($ln in Get-Content $envPath -Encoding UTF8) {
        if ($ln -match '^MCP_TUNNEL_NAME=(.+)$') { $firstName = $matches[1].Trim(); break }
    }
}
$machineSuffix = Get-MachineSuffix
if (-not $firstName) {
    # No recorded name: use this machine's generated default (current scheme, then legacy, then bare).
    foreach ($cand in @("$DEFAULT_NAME-$machineSuffix", "$DEFAULT_NAME-$(Get-LegacyMachineSuffix)", $DEFAULT_NAME)) {
        if ($existingNames -contains $cand) { $firstName = $cand; break }
    }
    if (-not $firstName) { $firstName = "$DEFAULT_NAME-$machineSuffix" }
}
Write-Host ("[3/5] first tunnel (read only, never changed): " + $firstName)

# Derive the second name. Explicit -SecondName wins; else firstName + "-r2". A chosen/derived
# name that is identifying is discarded for a machine-safe default + "-r2" (privacy guard).
if ($SecondName) {
    $target = $SecondName
} else {
    $target = "$firstName-$SECOND_SUFFIX"
}
if (Test-IdentifyingTunnelName $target) {
    $safe = "$DEFAULT_NAME-$machineSuffix-$SECOND_SUFFIX"
    Write-Host ("      the derived second name ('$target') is identifying -> using a private name: $safe")
    $target = $safe
}
if ($target -eq $firstName) {
    Write-Host "      ERROR: the second tunnel name equals the first ('$firstName'). They must differ so"
    Write-Host "      the two are independent tunnels. Pass a distinct -SecondName."
    exit 1
}
Write-Host ("      second tunnel id to use: " + $target)

# --- 4. ensure the SECOND tunnel + its port exist (idempotent). Only ever touches $target. -----
$reuse = ($existingNames -contains $target)
if ($reuse) {
    Write-Host "[4/5] reusing existing second tunnel: $target"
} else {
    Write-Host "[4/5] creating second tunnel '$target' (separate id; the first tunnel is left alone)..."
    $createOut = Dt create $target
    $createExit = $LASTEXITCODE
    if ($createExit -ne 0) {
        $createMsg = ($createOut -join "`n")
        if ($createMsg -match 'already exists|already in use|conflict|taken') {
            Write-Host "      '$target' already exists in the global namespace -> reusing it."
        } else {
            Write-Host "      ERROR: devtunnel create failed (exit ${createExit}):"
            Write-Host "      $createMsg"
            Write-Host "      Nothing on the first tunnel was changed. If login/permission, run: devtunnel user login"
            exit 1
        }
    }
}

if (-not ((Dt port list $target) -match ("\b" + [string]$Port + "\b"))) {
    Write-Host "      adding port $Port to '$target'..."
    $portOut = Dt port create $target -p $Port --protocol http
    $portExit = $LASTEXITCODE
    if ($portExit -ne 0) {
        $portMsg = ($portOut -join "`n")
        if ($portMsg -match 'already exists|already in use|conflict') {
            Write-Host "      port $Port already present on '$target' (ok)."
        } else {
            Write-Host "      ERROR: devtunnel port create failed (exit ${portExit}):"
            Write-Host "      $portMsg"
            exit 1
        }
    }
}

# --- 4b. MATCH THE FIRST TUNNEL'S ACCESS (same auth path). Read the first tunnel's grant and
# apply the SAME one to the second, so the local API key behaves identically on both URLs. We
# only ever ADD a grant to the SECOND tunnel; we never change the first.
function Test-AnonymousInListing([string]$text) { return [bool]($text -match '\+\s*Anonymous\b') }
function Test-TenantInListing([string]$text) { return [bool]($text -match '\+\s*(?:AAD\s+)?Tenants?\b') }
function Get-AccessGrantFromListing([string]$text) {
    if (Test-AnonymousInListing $text) { return "anonymous" }
    if (Test-TenantInListing $text) { return "tenant" }
    return "none"
}
function Read-AccessLevel([string]$t, [int]$p) {
    if ($p) { $o = Dt access list $t -p $p } else { $o = Dt access list $t }
    if ($LASTEXITCODE -ne 0) { return $null }
    return (@($o) -join "`n")
}
function Get-TunnelAccessGrant([string]$t, [int]$p) {
    $a = Read-AccessLevel $t 0
    $b = Read-AccessLevel $t $p
    if (($null -eq $a) -or ($null -eq $b)) { return $null }
    return (Get-AccessGrantFromListing ($a + "`n" + $b))
}
$firstAccess = Get-TunnelAccessGrant $firstName $Port
if ($null -eq $firstAccess) {
    Write-Host "      WARN: could not read the FIRST tunnel's access grant, so the second is left with the"
    Write-Host "      owner-only default. Check 'devtunnel access list $firstName' and re-run if the second"
    Write-Host "      should be reachable by the same callers."
    $firstAccess = "unknown"
}
Write-Host ("      first tunnel's access grant (read only): " + $firstAccess)
if ($firstAccess -eq "anonymous") {
    Write-Host "      granting anonymous access on the SECOND tunnel to match the first..."
    [void](Dt access create $target --anonymous)
    [void](Dt access create $target -p $Port --anonymous)
} elseif ($firstAccess -eq "tenant") {
    Write-Host "      granting tenant-only access on the SECOND tunnel to match the first..."
    [void](Dt access create $target --tenant)
} elseif ($firstAccess -eq "none") {
    Write-Host "      the FIRST tunnel has NO access grant, so no remote client reaches it. Nothing to copy"
    Write-Host "      to the second; both tunnels are owner-only. Choose a grant on the first (quickstart A/T)"
    Write-Host "      and re-run this to mirror it. The second tunnel and its URL are still set up below."
}

# --- 4c. read back the SECOND tunnel's access (retry once; wording may vary). ------------------
function Test-AnyAllowEntry([string]$text) { return [bool]($text -match '(?m)^\s*(?:\d+:\s*)?\+\s*\S') }
$secondAccess = Get-TunnelAccessGrant $target $Port
for ($try = 0; ($null -eq $secondAccess) -and ($try -lt 2); $try++) {
    Start-Sleep -Seconds 2
    $secondAccess = Get-TunnelAccessGrant $target $Port
}
if ($firstAccess -eq "anonymous" -and $secondAccess -and $secondAccess -ne "anonymous") {
    Write-Host "      the anonymous grant did not read back on the second tunnel -> granting once more..."
    [void](Dt access create $target --anonymous)
    [void](Dt access create $target -p $Port --anonymous)
    $secondAccess = Get-TunnelAccessGrant $target $Port
}
if (($firstAccess -eq "anonymous" -or $firstAccess -eq "tenant") -and ($secondAccess -eq "none")) {
    $rawA = Read-AccessLevel $target 0
    $rawB = Read-AccessLevel $target $Port
    if (Test-AnyAllowEntry ([string]$rawA + "`n" + [string]$rawB)) {
        $secondAccess = $firstAccess
        Write-Host "      (an allow entry is present on the second tunnel; its wording was not recognised)"
    } else {
        Write-Host "  ERROR: '$firstAccess' access was chosen on the first tunnel but did not read back on the"
        Write-Host "         second. Check: devtunnel access list $target  and  devtunnel access list $target -p $Port"
        exit 4
    }
}
if ($firstAccess -eq "none") {
    Write-Host "[5/5] NOTE: no access grant on either tunnel; the second's URL is recorded but no remote"
    Write-Host "      client can connect until a grant is chosen on the first and mirrored here."
}

# --- 5. host the SECOND tunnel once to mint its public URL, then surface + record it. ----------
function Tunnel-Url($name) {
    $s = Dt show $name
    return ($s | Select-String -Pattern 'https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*' -AllMatches |
            ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } | Select-Object -First 1)
}
$url = Tunnel-Url $target
if (-not $url) {
    Write-Host "[5/5] hosting the second tunnel to obtain its public URL (a few seconds)..."
    Start-Process $script:DtExe -ArgumentList @("host", $target) -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $url = Tunnel-Url $target
        if ($url) { break }
    }
}
Write-Host ""
Write-Host "==================================================================="
if ($url) {
    Write-Host " SECOND Dev Tunnel READY. Public URL of the MCP server (port $Port):"
    Write-Host ""
    Write-Host "     $url"
    Write-Host ""
    Write-Host " This is a SECOND path to the same local server. Add it to muse as a fallback"
    Write-Host " URL (muse-side config -- out of scope for this script). The second tunnel id is:"
    Write-Host "     $target"
    $aw = $secondAccess
    if (-not $aw) { $aw = "unknown (could not be read back)" }
    Write-Host " Access this second tunnel ends with: $aw"
    Write-Host ""
    Write-Host " Rollback (removes ONLY the second tunnel; the first is untouched):"
    Write-Host "     devtunnel delete $target"
} else {
    Write-Host " Second tunnel '$target' is set up but no public URL appeared after hosting."
    Write-Host "==================================================================="
    Write-Host " Run  devtunnel host $target  in a separate window and look for the"
    Write-Host " https://...devtunnels.ms URL. If login/permission, run: devtunnel user login"
    exit 1
}
Write-Host "==================================================================="

# Record the second tunnel's name + URL in .env as COMMENTED references (never a secret, never
# the first tunnel's keys). Separate keys so the first tunnel's MCP_TUNNEL_NAME/URL are untouched.
try {
    if (Test-Path $envPath) {
        $lines = @(Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -notmatch '^# devtunnel 2 \(auto\)|^MCP_TUNNEL_NAME_2=|^MCP_TUNNEL_URL_2=' })
        $lines += "# devtunnel 2 (auto) -- a SECOND public URL to the same MCP server; muse tries it when the first fails"
        $lines += "MCP_TUNNEL_NAME_2=$target"
        $lines += "MCP_TUNNEL_URL_2=$url"
        # No-BOM UTF-8 (ASCII would drop non-ASCII lines to '?'; PS 5.1 -Encoding UTF8 writes a BOM
        # that breaks the .env parser). Mirrors setup_devtunnel.ps1's write.
        [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "Recorded MCP_TUNNEL_NAME_2 and MCP_TUNNEL_URL_2 in .env (the first tunnel's keys are untouched)."
    } else {
        Write-Host "NOTE: .env not found at $envPath -- the second tunnel is up; its URL was not recorded."
    }
} catch {
    Write-Host "NOTE: failed to record the second tunnel URL in .env: $($_.Exception.Message)"
}
if ($firstAccess -eq "none") { exit 3 }
exit 0
