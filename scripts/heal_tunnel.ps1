# heal_tunnel.ps1 -- self-heal MCP_TUNNEL_NAME/MCP_TUNNEL_URL in .env at startup.
#
# PROBLEM: .env is sometimes copied between machines (e.g. a fresh clone/backup
# restore). MCP_TUNNEL_NAME and MCP_TUNNEL_URL are PER-DEVICE (a Dev Tunnel is
# owned by the account that created it) -- a receiving machine can end up with
# an .env naming a tunnel it does NOT own. `devtunnel host <name>` then fails
# with a scopes error, the tunnel is never served, and everything downstream
# (Copilot Studio connectivity) is broken. Users cannot be walked through this
# one-by-one, so this routine repairs it automatically, every time start_all
# runs, before the supervisor starts hosting.
#
# CONTRACT: read-only except for a minimal, surgical .env rewrite (only the
# MCP_TUNNEL_NAME line, and MCP_TUNNEL_URL only when it must change; and, for a
# .env carried from another machine, the MCP_TUNNEL_* keys env_portability.py
# names -- set aside as comments, exactly as setup_devtunnel.ps1 does). Never
# throws; every step is best-effort. If it cannot safely decide what to do, it
# no-ops rather than guessing. Every devtunnel CLI call is bounded so a hung or
# offline CLI cannot block the caller.
#
# THE SAME RULES AS setup_devtunnel.ps1 (2026-09-24). This script runs on EVERY
# start_all, and it kept an owned .env name -- or switched to the account's FIRST
# owned tunnel -- with no "is another machine hosting it" check, no privacy guard
# and no "did this .env come from another machine" check: the likeliest way a
# second PC with a copied .env hosted this PC's tunnel that day (57ad0d1). The
# rules and helpers now come from tunnel_name_util.ps1, shared with setup:
#   * never keep or adopt a tunnel `devtunnel show` reports as hosted while no
#     devtunnel host on THIS machine hosts it -- except this machine's own
#     generated names, which are this machine's by construction;
#   * never keep or adopt an identifying name (Test-IdentifyingTunnelName);
#   * a .env whose tunnel provably came from another machine
#     (Get-EnvTunnelProvenance: a host stamp naming another machine, or a
#     generated name with another machine's suffix) is not kept: its
#     machine-bound keys (tools/env_portability.py) are set aside;
#   * never switch to an ARBITRARY owned tunnel -- only to this machine's own.
#
# DECISION (see Get-TunnelHealAction, a pure function with no I/O):
#   0. .env provably came from another machine -> ADOPT_OWN: set its tunnel keys
#      aside and point at this machine's own tunnel when the account owns one
#      (URL changes, re-paste; notified); else SET_ASIDE: set them aside, name
#      this machine's own default (not created here) and notify that setup is
#      needed -- the supervisor then has nothing foreign to host.
#   1. MCP_TUNNEL_NAME is owned by this account:
#      1a. identifying, or hosted by another machine right now (and not this
#          machine's own name) -> RENAME_URL to this machine's own tunnel if the
#          account owns one, else REFUSED: nothing changed, notified.
#      1b. URL matches (or the real URL is unknown) -> no-op.
#      1c. URL is stale/blank and the tunnel's real URL is known -> URL_FIX:
#          rewrite only MCP_TUNNEL_URL, then notify (re-paste).
#   2. Not owned (or empty), but a USABLE owned tunnel's forwarding URL equals the
#      recorded MCP_TUNNEL_URL -> REPOINT: rewrite only MCP_TUNNEL_NAME. The
#      public URL is UNCHANGED. Silent. (Usable = not identifying, and not hosted
#      by another machine unless it is this machine's own.)
#   3. Not owned, no URL match -> RENAME_URL to this machine's own tunnel if the
#      account owns one (notified); otherwise SETUP_NEEDED -- never the account's
#      first tunnel, which with one account on two PCs is as likely the other
#      PC's as this one's.
#   4. The account owns no tunnel at all -> do NOT create one here (that is
#      setup_devtunnel.ps1's job); desktop-notify once that setup is needed.
#
# USAGE
#   powershell -File scripts\heal_tunnel.ps1            # heal for real
#   powershell -File scripts\heal_tunnel.ps1 -DryRun     # read-only preview; prints what
#                                                          would change, writes/notifies nothing
#
# ASCII / ENGLISH ONLY (cmd/console safe). No BOM (matches doctor.ps1/repair.ps1).
param(
    [switch]$DryRun
)
$ErrorActionPreference = "Continue"

# This script lives in <repo>\scripts; .env is at the REPO ROOT (one level up).
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = Split-Path -Parent $scriptDir
$envPath = Join-Path $root ".env"

# The rules setup_devtunnel.ps1 applies (machine identity, "hosted by another machine", the
# privacy guard, "came from another machine", env_portability's machine-bound keys). Functions
# only; no top-level side effects.
. (Join-Path $scriptDir "tunnel_name_util.ps1")

# -----------------------------------------------------------------------------
# devtunnel binary resolution -- mirrors supervisor.ps1 / doctor.ps1: prefer the
# winget-installed devtunnel.exe (kept current), else "devtunnel" on PATH.
# -----------------------------------------------------------------------------
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

# Bounded devtunnel invocation -- Start-Job + poll-with-deadline (same pattern as
# start_all.ps1's git-fetch timeout and doctor.ps1's Invoke-DevTunnelBounded): a
# hung or offline devtunnel CLI call times out instead of hanging the caller.
function Invoke-DevTunnelBounded([string[]]$dtArgs, [int]$timeoutSec) {
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

function Test-DevTunnelLoggedIn {
    $out = Invoke-DevTunnelBounded @('user', 'show') 6
    if (-not $out) { return $false }
    if ($out -match 'Not logged in' -or $out -match 'Login required') { return $false }
    if ($out -match 'Logged in') { return $true }
    return $false
}

# =============================================================================
# PURE helpers -- no devtunnel/.env I/O. Kept separate and side-effect-free so
# they can be unit-tested directly (dot-source this file and call them).
# =============================================================================

function Get-BareTunnelId([string]$id) {
    # "name.cluster" -> "name" (lowercased). Also safe to call on an already-bare
    # id (no dot present) -- returns it lowercased, unchanged in shape.
    if (-not $id) { return "" }
    return (($id -split '\.')[0]).ToLowerInvariant()
}

function Test-LooksLikeExecutableSuffix([string]$suffix) {
    # A real devtunnel cluster code (usw2, use2, jpe1, weu, asse, ...) is never a Windows
    # executable or script extension. Invoke-DevTunnelBounded merges stderr into the text
    # that gets parsed as data (`2>&1 | Out-String`), and PowerShell renders a native
    # command's stderr as a line starting with the executable name, e.g.
    # "devtunnel.exe : Error: ...". That line has the exact shape of a tunnel-id row
    # ("word.word "), so the id/cluster regex alone cannot tell them apart -- this
    # blacklist is the second, independent signal that rejects it.
    if (-not $suffix) { return $false }
    $exeSuffixes = @('exe', 'dll', 'com', 'bat', 'cmd', 'ps1', 'psm1', 'msi', 'vbs', 'scr', 'msc')
    return $exeSuffixes -contains $suffix.ToLowerInvariant()
}

function Get-OwnedTunnelIdsFromListOutput([string]$listOut) {
    # PURE parser for `devtunnel list` output AS RECEIVED FROM Invoke-DevTunnelBounded,
    # i.e. already merged with stderr. A genuine tunnel-id row starts the line with
    # "<id>.<cluster>" followed by whitespace and then further table columns (never a
    # colon). PowerShell's rendering of a merged native-command error line matches the
    # id/cluster SHAPE too ("devtunnel.exe : Error: ..." -> id "devtunnel", cluster "exe")
    # so a captured candidate is only accepted once it also passes two independent,
    # positive checks: the cluster segment must not be an executable/script suffix (see
    # Test-LooksLikeExecutableSuffix), and the character immediately after the id must not
    # be ":" -- a real listing column is separated by plain whitespace, the error line's
    # separator is " : ".
    if (-not $listOut) { return @() }
    $ids = @()
    foreach ($line in ($listOut -split "`r?`n")) {
        if ($line -match '^\s*([a-z0-9][a-z0-9-]+)\.([a-z0-9]+)(\s+)(\S)') {
            $bareId = $matches[1]
            $cluster = $matches[2]
            $nextChar = $matches[4]
            if (Test-LooksLikeExecutableSuffix $cluster) { continue }
            if ($nextChar -eq ':') { continue }
            $ids += "$bareId.$cluster"
        }
    }
    return $ids
}

function Test-TunnelNameOwned([string]$name, [array]$ownedIds) {
    # $ownedIds may hold bare or full ("name.cluster") ids -- comparison is
    # always done on the bare form on both sides, so either shape works.
    if (-not $name) { return $false }
    $bareName = Get-BareTunnelId $name
    if (-not $bareName) { return $false }
    foreach ($o in $ownedIds) {
        if ((Get-BareTunnelId $o) -eq $bareName) { return $true }
    }
    return $false
}

function Normalize-TunnelUrl([string]$u) {
    # Compare by host+port only -- ignore trailing path/slash differences.
    if (-not $u) { return "" }
    try {
        $uri = [Uri]$u.Trim()
        return ($uri.Host + ":" + $uri.Port).ToLowerInvariant()
    } catch {
        return ($u.Trim().TrimEnd('/')).ToLowerInvariant()
    }
}

function Get-HealEntryField($Entry, [string]$Field, $Default) {
    # PURE. An optional field of an $Owned entry ($Default when the caller did not supply it).
    if ($Entry -and $Entry.PSObject.Properties[$Field]) { return $Entry.$Field }
    return $Default
}
function Test-HealEntryHostedElsewhere($Entry) {
    # PURE. `devtunnel show` counted a host, and it is not a devtunnel host on this machine. An
    # unknown count ($null / not supplied) is not evidence of anything.
    $hc = Get-HealEntryField $Entry 'HostConnections' $null
    if ($null -eq $hc) { return $false }
    return ([int]$hc -ge 1 -and -not [bool](Get-HealEntryField $Entry 'HostedHere' $false))
}
function Test-IsOwnDefaultTunnel([string]$Id, [string[]]$OwnDefaults) {
    # PURE. One of THIS machine's generated names (Get-OwnDefaultTunnelNames).
    if (-not $Id) { return $false }
    $b = Get-BareTunnelId $Id
    foreach ($d in @($OwnDefaults)) { if ($d -and ((Get-BareTunnelId $d) -eq $b)) { return $true } }
    return $false
}
function Test-HealEntryUsable($Entry, [string[]]$OwnDefaults) {
    # PURE. May this machine keep or adopt the tunnel? Not an identifying name, and not one
    # another machine is hosting right now -- unless it is this machine's own name.
    if ([bool](Get-HealEntryField $Entry 'Identifying' $false)) { return $false }
    if (Test-IsOwnDefaultTunnel $Entry.Id $OwnDefaults) { return $true }
    return (-not (Test-HealEntryHostedElsewhere $Entry))
}
function Get-TunnelHealAction {
    # PURE decision function. $Owned is an array of PSCustomObject with:
    #   Id  = the owned tunnel's id (bare or full -- either works)
    #   Url = that tunnel's forwarding URL, or "" if unknown/unresolved
    #   optional: HostConnections (int, or $null = unknown), HostedHere (bool: a devtunnel host
    #   on THIS machine hosts it), Identifying (bool: Test-IdentifyingTunnelName)
    # -ForeignReason: why .env's tunnel came from another machine (Get-EnvTunnelProvenance), or "".
    # -OwnDefaults: this machine's own generated names (Get-OwnDefaultTunnelNames), preferred first.
    # Returns a PSCustomObject: Action ('noop'|'repoint'|'rename_url'|'setup_needed'|'url_fix'|
    # 'adopt_own'|'set_aside'|'refused'), TargetId, TargetUrl, Note.
    param(
        [string]$Name,
        [string]$Url,
        [array]$Owned,
        [string]$ForeignReason = "",
        [string[]]$OwnDefaults = @()
    )
    $ownedList = @($Owned | Where-Object { $_ })
    $ownedIds = @($ownedList | ForEach-Object { $_.Id })

    # This machine's own tunnel, if the account owns one whose URL is known: the ONLY tunnel
    # this script ever switches to (never "the first one in the list").
    $own = $null
    foreach ($d in @($OwnDefaults)) {
        $own = $ownedList | Where-Object { $d -and ((Get-BareTunnelId $_.Id) -eq (Get-BareTunnelId $d)) -and $_.Url } | Select-Object -First 1
        if ($own) { break }
    }

    # 0. .env's tunnel was made on another machine: neither kept nor used to match a URL.
    if ($ForeignReason) {
        if ($own) {
            return [PSCustomObject]@{
                Action    = 'adopt_own'
                TargetId  = $own.Id
                TargetUrl = $own.Url
                Note      = ".env came from another machine ($ForeignReason); its tunnel settings were set aside and this machine's own tunnel $($own.Id) is used; URL changed, re-paste required"
            }
        }
        $fallbackName = ""
        if (@($OwnDefaults).Count -gt 0) { $fallbackName = @($OwnDefaults)[0] }
        return [PSCustomObject]@{
            Action    = 'set_aside'
            TargetId  = $fallbackName
            TargetUrl = ''
            Note      = ".env came from another machine ($ForeignReason); its tunnel settings were set aside -- run scripts\setup_devtunnel.ps1 (quickstart STEP 4) to create this machine's own tunnel"
        }
    }

    # 1. N is owned. That alone does not guarantee MCP_TUNNEL_URL is correct --
    #    .env can be stale (e.g. NAME and URL pasted in from different tunnels
    #    at different times), so validate the recorded URL against this owned
    #    tunnel's real forwarding URL before declaring "nothing to do".
    if ($Name -and (Test-TunnelNameOwned $Name $ownedIds)) {
        $bareName = Get-BareTunnelId $Name
        $match = $ownedList | Where-Object { (Get-BareTunnelId $_.Id) -eq $bareName } | Select-Object -First 1
        # 1a. Owned is not enough to KEEP it: not an identifying name, and not a tunnel another
        #     machine is hosting right now (this machine's own names excepted).
        if ($match -and -not (Test-HealEntryUsable $match $OwnDefaults)) {
            $why = "another machine is hosting it right now"
            if ([bool](Get-HealEntryField $match 'Identifying' $false)) { $why = "its name is identifying" }
            if ($own -and ((Get-BareTunnelId $own.Id) -ne $bareName)) {
                return [PSCustomObject]@{
                    Action    = 'rename_url'
                    TargetId  = $own.Id
                    TargetUrl = $own.Url
                    Note      = "not keeping $Name ($why); switched to this machine's own tunnel $($own.Id); URL changed, re-paste required"
                }
            }
            return [PSCustomObject]@{
                Action    = 'refused'
                TargetId  = $Name
                TargetUrl = $Url
                Note      = "tunnel $Name is not safe to host here ($why) and this machine has no tunnel of its own -- run scripts\setup_devtunnel.ps1"
            }
        }
        if ($match -and $match.Url) {
            if ((-not $Url) -or ((Normalize-TunnelUrl $Url) -ne (Normalize-TunnelUrl $match.Url))) {
                return [PSCustomObject]@{
                    Action    = 'url_fix'
                    TargetId  = $Name
                    TargetUrl = $match.Url
                    Note      = "corrected stale MCP_TUNNEL_URL for owned tunnel $Name; re-paste required"
                }
            }
        }
        # URL matches, or the owned entry's real URL is unknown/empty so it
        # cannot be safely corrected -- never churn on unknown data.
        return [PSCustomObject]@{
            Action    = 'noop'
            TargetId  = $Name
            TargetUrl = $Url
            Note      = "tunnel $Name is owned; nothing to do"
        }
    }

    # 4. The account owns no tunnel at all.
    if ($ownedList.Count -eq 0) {
        return [PSCustomObject]@{
            Action    = 'setup_needed'
            TargetId  = ''
            TargetUrl = ''
            Note      = "no owned tunnel exists -- run scripts\setup_devtunnel.ps1"
        }
    }

    # 2. Not owned (or empty) -- but a USABLE owned tunnel's URL matches the recorded
    #    URL -> silent, zero-disruption repoint (URL preserved).
    if ($Url) {
        $normU = Normalize-TunnelUrl $Url
        foreach ($o in $ownedList) {
            if (-not (Test-HealEntryUsable $o $OwnDefaults)) { continue }
            if ($o.Url -and ((Normalize-TunnelUrl $o.Url) -eq $normU)) {
                return [PSCustomObject]@{
                    Action    = 'repoint'
                    TargetId  = $o.Id
                    TargetUrl = $Url
                    Note      = "repointed to owned tunnel $($o.Id), URL preserved"
                }
            }
        }
    }

    # 3. Not owned, no URL match. Only THIS machine's own tunnel is a candidate: with one
    #    account signed in on two PCs, "the first tunnel in the list" is as likely the other
    #    PC's as this one's (that was $ownedList[0] until 2026-09-24).
    if ($own) {
        return [PSCustomObject]@{
            Action    = 'rename_url'
            TargetId  = $own.Id
            TargetUrl = $own.Url
            Note      = "switched to this machine's own tunnel $($own.Id); URL changed, re-paste required"
        }
    }
    return [PSCustomObject]@{
        Action    = 'setup_needed'
        TargetId  = ''
        TargetUrl = ''
        Note      = "no tunnel of this machine's own exists (not adopting another tunnel of the account) -- run scripts\setup_devtunnel.ps1"
    }
}

function Set-EnvTunnelAsideAndPoint {
    # For a .env carried from another machine: the machine-bound MCP_TUNNEL_* keys ($Keys, from
    # env_portability.py) become comments (ConvertTo-EnvLinesWithKeysAside, as setup_devtunnel.ps1
    # does), then MCP_TUNNEL_NAME=<this machine's tunnel> is appended -- and, when its URL is known,
    # MCP_TUNNEL_URL and the MCP_TUNNEL_HOST stamp naming this machine. BOM state preserved;
    # temp file + move. $false (nothing written) if an active MCP_TUNNEL_NAME line would remain.
    param([Parameter(Mandatory = $true)][string]$EnvPath, [string[]]$Keys, [string]$NewName, [string]$NewUrl = "")
    try {
        $bytes = [System.IO.File]::ReadAllBytes($EnvPath)
        $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        $enc = New-Object System.Text.UTF8Encoding($false)
        $off = 0
        if ($hasBom) { $off = 3 }
        $lines = @(($enc.GetString($bytes, $off, $bytes.Length - $off)) -split "`r?`n")
        if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq "") { $lines = @($lines | Select-Object -First ($lines.Count - 1)) }
        # NOT wrapped in @(): the helper returns its array with `return ,$out`, and @() around that
        # makes a one-element array holding it -- measured: the .env became "System.Object[]".
        $out = ConvertTo-EnvLinesWithKeysAside $lines $Keys "set aside by heal_tunnel.ps1: made on another machine, not valid on this one"
        if ($out -isnot [array] -or $out.Count -lt $lines.Count) { return $false }
        if ($out | Where-Object { $_ -match '^\s*MCP_TUNNEL_(NAME|URL|HOST)\s*=' }) { return $false }
        if ($NewName) { $out += "MCP_TUNNEL_NAME=$NewName" }
        if ($NewUrl) {
            $out += "MCP_TUNNEL_URL=$NewUrl"
            $out += ("MCP_TUNNEL_HOST=" + (Get-ThisHost))
        }
        $outBytes = $enc.GetBytes((($out -join "`r`n") + "`r`n"))
        if ($hasBom) { $outBytes = [byte[]](0xEF, 0xBB, 0xBF) + $outBytes }
        $tmpPath = "$EnvPath.heal_tmp_$([Guid]::NewGuid().ToString('N'))"
        [System.IO.File]::WriteAllBytes($tmpPath, $outBytes)
        Move-Item -LiteralPath $tmpPath -Destination $EnvPath -Force
        return $true
    } catch { return $false }
}

function Get-OwnedTunnelEntry([string]$Id) {
    # One `devtunnel show`: the forwarding URL, the host-connection count, whether a devtunnel
    # host on this machine hosts it, and whether the name is identifying.
    $showOut = Invoke-DevTunnelBounded @('show', (Get-BareTunnelId $Id)) 8
    $url = ""
    $hc = $null
    if ($showOut) {
        $m = [regex]::Match($showOut, 'https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*')
        if ($m.Success) { $url = $m.Value }
        $hc = Get-HostConnectionsFromShow ($showOut -split "`r?`n")
    }
    $here = $false
    if ($null -ne $hc -and $hc -ge 1) { $here = [bool](Test-ThisMachineHostsTunnel $Id) }
    return [PSCustomObject]@{ Id = $Id; Url = $url; HostConnections = $hc; HostedHere = $here
                              Identifying = [bool](Test-IdentifyingTunnelName $Id) }
}

function Update-EnvTunnelFields {
    # Surgical, atomic .env rewrite: touches ONLY the MCP_TUNNEL_NAME line (and
    # the MCP_TUNNEL_URL line, only when -NewUrl is non-empty) -- every other
    # line, the text encoding, and the BOM state are preserved byte-for-byte.
    # Written via a temp file + Move-Item so a crash mid-write cannot corrupt .env.
    param(
        [Parameter(Mandatory = $true)][string]$EnvPath,
        [Parameter(Mandatory = $true)][string]$NewName,
        [string]$NewUrl = ""
    )
    if (-not (Test-Path -LiteralPath $EnvPath)) { return $false }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($EnvPath)
    } catch { return $false }

    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $offset = 0
    if ($hasBom) { $offset = 3 }
    $text = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)

    if ($text -notmatch '(?m)^\s*MCP_TUNNEL_NAME\s*=') { return $false }
    $text = $text -replace '(?m)^(\s*MCP_TUNNEL_NAME\s*=).*$', ('$1' + $NewName)

    if ($NewUrl) {
        if ($text -match '(?m)^\s*MCP_TUNNEL_URL\s*=') {
            $text = $text -replace '(?m)^(\s*MCP_TUNNEL_URL\s*=).*$', ('$1' + $NewUrl)
        } else {
            $sep = ""
            if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $sep = "`r`n" }
            $text = $text + $sep + "MCP_TUNNEL_URL=$NewUrl`r`n"
        }
    }

    $outBytes = $encoding.GetBytes($text)
    if ($hasBom) {
        $preamble = [byte[]](0xEF, 0xBB, 0xBF)
        $outBytes = $preamble + $outBytes
    }

    $tmpPath = "$EnvPath.heal_tmp_$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::WriteAllBytes($tmpPath, $outBytes)
        Move-Item -LiteralPath $tmpPath -Destination $EnvPath -Force
        return $true
    } catch {
        try { if (Test-Path -LiteralPath $tmpPath) { Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue } } catch { }
        return $false
    }
}

function Read-EnvValue([string]$path, [string]$key) {
    if (-not (Test-Path -LiteralPath $path)) { return "" }
    $m = (Get-Content -LiteralPath $path | Where-Object { $_ -match "^\s*$([regex]::Escape($key))\s*=" } | Select-Object -First 1)
    if ($m) { return ($m -replace "^\s*$([regex]::Escape($key))\s*=\s*", "").Trim() }
    return ""
}

function Send-TunnelHealNotice([string]$title, [string]$body) {
    # Best-effort desktop toast via the project's existing notify_ops helper.
    # Mirrors supervisor.ps1's own invocation of the same helper.
    try {
        $py = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { $py = "python" }
        # $root IS PASSED AS sys.argv[1], NOT SPLICED INTO THE CODE STRING (fix, 2026-09-24).
        # An install path containing an apostrophe (e.g. D:\repos\o'brien-clone) broke
        # the generated Python raw-string literal `r'$root'` with a SyntaxError -- and because
        # this whole call sits inside the outer try/catch, the toast just silently never fired
        # for every operator whose path has one. sys.argv is passed as a separate process
        # argument, so no character in $root can break out of the -c code string.
        $code = "import sys; sys.path.insert(0, sys.argv[1]); from tools.notify_ops import notify_desktop; notify_desktop('$title', '$body')"
        & $py -c $code $root 2>$null | Out-Null
    } catch { }
}

# =============================================================================
# Driver
# =============================================================================
function Invoke-TunnelHeal([switch]$DryRunMode) {
    try {
        if (-not (Test-Path -LiteralPath $envPath)) {
            Write-Host "[heal_tunnel] no .env found -- no-op"
            return
        }

        # 0. devtunnel present and logged in -- else NO-OP (cannot manage tunnels).
        $verOut = Invoke-DevTunnelBounded @('--version') 6
        if (-not $verOut -or ($verOut -notmatch 'Tunnel CLI version')) {
            Write-Host "[heal_tunnel] devtunnel CLI not available -- no-op"
            return
        }
        if (-not (Test-DevTunnelLoggedIn)) {
            Write-Host "[heal_tunnel] devtunnel not logged in -- no-op"
            return
        }

        $N = Read-EnvValue $envPath "MCP_TUNNEL_NAME"
        $U = Read-EnvValue $envPath "MCP_TUNNEL_URL"
        $H = Read-EnvValue $envPath "MCP_TUNNEL_HOST"
        # Did this .env's tunnel come from another machine? The same test setup_devtunnel.ps1's
        # section 0 and bootstrap.py make (Get-EnvTunnelProvenance).
        $prov = Get-EnvTunnelProvenance -RecordedHost $H -RecordedName $N
        $foreignWhy = [string]$prov.ForeignReason
        $ownDefaults = @(Get-OwnDefaultTunnelNames)

        # 2. owned tunnel ids from `devtunnel list`.
        $listOut = Invoke-DevTunnelBounded @('list') 8
        if (-not $listOut) {
            Write-Host "[heal_tunnel] 'devtunnel list' failed/timed out -- no-op"
            return
        }
        $rawIds = @(Get-OwnedTunnelIdsFromListOutput $listOut)
        if ($rawIds.Count -eq 0 -and ($listOut -notmatch 'Found 0 tunnels')) {
            # No ids parsed AND the output did not clearly say "0 tunnels" --
            # treat as an unparseable/failed listing, not a genuinely empty
            # account, so a parse glitch can never masquerade as "no tunnel".
            Write-Host "[heal_tunnel] could not parse 'devtunnel list' output -- no-op"
            return
        }
        $ownedIds = @($rawIds | ForEach-Object { Get-BareTunnelId $_ } | Select-Object -Unique)

        # 3/4. Resolve the URL(s) needed to decide, then hand off to the pure
        #    decision function -- it (not this caller) decides noop vs. url_fix
        #    vs. repoint vs. rename_url. In the owned case, resolve ONLY that
        #    one tunnel's real URL (a single cheap 'show' call) so a stale
        #    MCP_TUNNEL_URL can be corrected even when the name itself is
        #    already right. In the not-owned case, resolve every owned
        #    tunnel's URL as before so repoint/rename_url can find a match.
        #    Each entry also carries the host count, whether this machine hosts it and whether
        #    the name is identifying (Get-OwnedTunnelEntry), which the decision now needs. This
        #    machine's own generated names are always looked at: they are the only tunnel this
        #    script may switch to.
        $owned = @()
        $ownOwned = @($ownedIds | Where-Object { Test-IsOwnDefaultTunnel $_ $ownDefaults })
        if ($foreignWhy) {
            foreach ($id in $ownOwned) { $owned += Get-OwnedTunnelEntry $id }
        } elseif ($N -and (Test-TunnelNameOwned $N $ownedIds)) {
            $owned = @(Get-OwnedTunnelEntry $N)
            foreach ($id in $ownOwned) {
                if ((Get-BareTunnelId $id) -ne (Get-BareTunnelId $N)) { $owned += Get-OwnedTunnelEntry $id }
            }
        } else {
            foreach ($id in $ownedIds) { $owned += Get-OwnedTunnelEntry $id }
        }

        $decision = Get-TunnelHealAction -Name $N -Url $U -Owned $owned -ForeignReason $foreignWhy -OwnDefaults $ownDefaults

        switch ($decision.Action) {
            'url_fix' {
                if ($DryRunMode) {
                    Write-Host "[heal_tunnel] DRY RUN: would correct MCP_TUNNEL_URL for $N -> $($decision.TargetUrl) (re-paste required)"
                } else {
                    if (Update-EnvTunnelFields -EnvPath $envPath -NewName $N -NewUrl $decision.TargetUrl) {
                        Write-Host "[heal_tunnel] $($decision.Note)"
                        Send-TunnelHealNotice "Dev Tunnel URL corrected" "Your dev tunnel URL in .env was stale and has been corrected to $($decision.TargetUrl) -- re-paste it into the Copilot Studio MCP connector."
                    } else {
                        Write-Host "[heal_tunnel] url_fix decided but .env rewrite failed -- no-op"
                    }
                }
            }
            'repoint' {
                if ($DryRunMode) {
                    Write-Host "[heal_tunnel] DRY RUN: would repoint MCP_TUNNEL_NAME $N -> $($decision.TargetId), URL preserved"
                } else {
                    if (Update-EnvTunnelFields -EnvPath $envPath -NewName $decision.TargetId) {
                        Write-Host "[heal_tunnel] $($decision.Note)"
                    } else {
                        Write-Host "[heal_tunnel] repoint decided but .env rewrite failed -- no-op"
                    }
                }
            }
            'rename_url' {
                if ($DryRunMode) {
                    Write-Host "[heal_tunnel] DRY RUN: would set MCP_TUNNEL_NAME $N -> $($decision.TargetId) and MCP_TUNNEL_URL -> $($decision.TargetUrl) (re-paste required)"
                } else {
                    if (Update-EnvTunnelFields -EnvPath $envPath -NewName $decision.TargetId -NewUrl $decision.TargetUrl) {
                        Write-Host "[heal_tunnel] $($decision.Note)"
                        Send-TunnelHealNotice "Dev Tunnel URL changed" "Your dev tunnel now points to your own account tunnel. New URL: $($decision.TargetUrl) -- re-paste it into the Copilot Studio MCP connector."
                    } else {
                        Write-Host "[heal_tunnel] rename_url decided but .env rewrite failed -- no-op"
                    }
                }
            }
            { $_ -in @('adopt_own', 'set_aside') } {
                if ($DryRunMode) {
                    Write-Host "[heal_tunnel] DRY RUN: would set aside .env's tunnel keys ($foreignWhy) and set MCP_TUNNEL_NAME -> $($decision.TargetId)"
                } else {
                    # Which keys go is env_portability.py's decision, as in setup_devtunnel.ps1.
                    # Unanswerable -> nothing is changed (no guessing), and the operator is told.
                    $aside = Get-MachineBoundTunnelKeys $envPath
                    if ($null -eq $aside) {
                        Write-Host "[heal_tunnel] .env came from another machine ($foreignWhy), but tools\env_portability.py could not be run to decide what to set aside -- nothing changed"
                        Send-TunnelHealNotice "Dev Tunnel belongs to another PC" "The .env on this PC names a tunnel made on another PC. Run quickstart.bat (STEP 4) or scripts\setup_devtunnel.ps1 to give this PC its own tunnel."
                    } elseif (Set-EnvTunnelAsideAndPoint -EnvPath $envPath -Keys $aside -NewName $decision.TargetId -NewUrl $decision.TargetUrl) {
                        Write-Host "[heal_tunnel] $($decision.Note)"
                        if ($decision.Action -eq 'adopt_own') {
                            Send-TunnelHealNotice "Dev Tunnel URL changed" "The .env on this PC came from another PC, so this PC now uses its own tunnel. New URL: $($decision.TargetUrl) -- paste it into the Copilot Studio MCP connector that should reach this PC."
                        } else {
                            Send-TunnelHealNotice "Dev Tunnel setup needed" "The .env on this PC came from another PC; its tunnel settings were set aside. Run quickstart.bat (STEP 4) or scripts\setup_devtunnel.ps1 to give this PC its own tunnel."
                        }
                    } else {
                        Write-Host "[heal_tunnel] $($decision.Action) decided but .env rewrite failed -- no-op"
                    }
                }
            }
            'refused' {
                Write-Host "[heal_tunnel] $($decision.Note)"
                if (-not $DryRunMode) {
                    Send-TunnelHealNotice "Dev Tunnel needs attention" "The tunnel named in .env is not safe to host on this PC (another PC is hosting it, or its name is identifying). Run scripts\setup_devtunnel.ps1 to give this PC its own tunnel."
                }
            }
            'setup_needed' {
                if ($DryRunMode) {
                    Write-Host "[heal_tunnel] DRY RUN: would notify that no owned tunnel exists (run setup_devtunnel.ps1)"
                } else {
                    Write-Host "[heal_tunnel] $($decision.Note)"
                    Send-TunnelHealNotice "Dev Tunnel setup needed" "Your account has no dev tunnel yet. Run: powershell -File scripts\setup_devtunnel.ps1"
                }
            }
            default {
                Write-Host "[heal_tunnel] $($decision.Note)"
            }
        }
    } catch {
        # Self-heal is best-effort only; it must never throw out to the caller.
        Write-Host "[heal_tunnel] unexpected error -- no-op: $_"
    }
}

# Dot-source guard: only run the heal automatically when this script is
# invoked directly (`powershell -File heal_tunnel.ps1` / `& .\heal_tunnel.ps1`),
# not when it is dot-sourced (`. .\heal_tunnel.ps1`) to reuse its pure
# functions -- e.g. Get-TunnelHealAction -- in unit tests without triggering
# real devtunnel/.env I/O. $MyInvocation.InvocationName is literally '.' only
# for a dot-sourced call.
if ($MyInvocation.InvocationName -ne '.') {
    Invoke-TunnelHeal -DryRunMode:$DryRun
}
