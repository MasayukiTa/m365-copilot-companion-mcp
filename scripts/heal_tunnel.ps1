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
# MCP_TUNNEL_NAME line, and MCP_TUNNEL_URL only when it must change). Never
# throws; every step is best-effort. If it cannot safely decide what to do, it
# no-ops rather than guessing. Every devtunnel CLI call is bounded so a hung or
# offline CLI cannot block the caller.
#
# DECISION (see Get-TunnelHealAction, a pure function with no I/O):
#   1. MCP_TUNNEL_NAME is owned by this account -> ALREADY CORRECT. No-op.
#      (this is the case on a machine that has always been correctly set up --
#      it must be left completely untouched.)
#   2. Not owned (or empty), but an owned tunnel's forwarding URL equals the
#      recorded MCP_TUNNEL_URL -> REPOINT: rewrite only MCP_TUNNEL_NAME to that
#      owned tunnel's id. The public URL is UNCHANGED, so nothing needs to be
#      re-pasted into Copilot Studio. Silent, zero-disruption.
#   3. Not owned, no URL match, but the account owns at least one tunnel ->
#      switch to that owned tunnel: rewrite MCP_TUNNEL_NAME and MCP_TUNNEL_URL,
#      then desktop-notify that the URL changed and must be re-pasted.
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

# -----------------------------------------------------------------------------
# devtunnel binary resolution -- mirrors supervisor.ps1 / doctor.ps1: prefer the
# winget-installed devtunnel.exe (kept current), else "devtunnel" on PATH.
# -----------------------------------------------------------------------------
$DevTunnel = "devtunnel"
$wingetDt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
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

function Get-TunnelHealAction {
    # PURE decision function. $Owned is an array of PSCustomObject with:
    #   Id  = the owned tunnel's id (bare or full -- either works)
    #   Url = that tunnel's forwarding URL, or "" if unknown/unresolved
    # Returns a PSCustomObject: Action ('noop'|'repoint'|'rename_url'|'setup_needed'),
    # TargetId, TargetUrl, Note.
    param(
        [string]$Name,
        [string]$Url,
        [array]$Owned
    )
    $ownedList = @($Owned)
    $ownedIds = @($ownedList | ForEach-Object { $_.Id })

    # 1. Already correct -- N is owned. Left completely untouched.
    if ($Name -and (Test-TunnelNameOwned $Name $ownedIds)) {
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

    # 2. Not owned (or empty) -- but an owned tunnel's URL matches the recorded
    #    URL -> silent, zero-disruption repoint (URL preserved).
    if ($Url) {
        $normU = Normalize-TunnelUrl $Url
        foreach ($o in $ownedList) {
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

    # 3. Not owned, no URL match, but the account owns at least one tunnel.
    $first = $ownedList[0]
    return [PSCustomObject]@{
        Action    = 'rename_url'
        TargetId  = $first.Id
        TargetUrl = $first.Url
        Note      = "switched to owned tunnel $($first.Id); URL changed, re-paste required"
    }
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
        $code = "import sys; sys.path.insert(0, r'$root'); from tools.notify_ops import notify_desktop; notify_desktop('$title', '$body')"
        & $py -c $code 2>$null | Out-Null
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

        # 2. owned tunnel ids from `devtunnel list`.
        $listOut = Invoke-DevTunnelBounded @('list') 8
        if (-not $listOut) {
            Write-Host "[heal_tunnel] 'devtunnel list' failed/timed out -- no-op"
            return
        }
        $rawIds = @($listOut -split "`r?`n" | ForEach-Object {
            if ($_ -match '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s') { $matches[1] }
        } | Where-Object { $_ })
        if ($rawIds.Count -eq 0 -and ($listOut -notmatch 'Found 0 tunnels')) {
            # No ids parsed AND the output did not clearly say "0 tunnels" --
            # treat as an unparseable/failed listing, not a genuinely empty
            # account, so a parse glitch can never masquerade as "no tunnel".
            Write-Host "[heal_tunnel] could not parse 'devtunnel list' output -- no-op"
            return
        }
        $ownedIds = @($rawIds | ForEach-Object { Get-BareTunnelId $_ } | Select-Object -Unique)

        # 3. Already correct? Check BEFORE resolving any URLs -- this is the
        #    common case and must stay a pure no-op with no further devtunnel calls.
        if ($N -and (Test-TunnelNameOwned $N $ownedIds)) {
            Write-Host "[heal_tunnel] tunnel $N is owned; nothing to do"
            return
        }

        # 4. Resolve each owned tunnel's forwarding URL so the decision function
        #    can compare against the recorded MCP_TUNNEL_URL.
        $owned = @()
        foreach ($id in $ownedIds) {
            $showOut = Invoke-DevTunnelBounded @('show', $id) 8
            $url = ""
            if ($showOut) {
                $m = [regex]::Match($showOut, 'https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*')
                if ($m.Success) { $url = $m.Value }
            }
            $owned += [PSCustomObject]@{ Id = $id; Url = $url }
        }

        $decision = Get-TunnelHealAction -Name $N -Url $U -Owned $owned

        switch ($decision.Action) {
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

Invoke-TunnelHeal -DryRunMode:$DryRun
