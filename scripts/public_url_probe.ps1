# public_url_probe.ps1 -- ALWAYS-ON probe of the PUBLIC Dev Tunnel path and the tunnel's
# sign-in / host-connection state. It raises a VISIBLE alert within 60 s of a failure and the
# alert NAMES WHICH CAUSE is at fault (not logged in / no host / local server down / public
# path down), so the operator is sent to the right fix instead of 'it's down somewhere'.
#
# WHY THIS EXISTS (measured 2026-11-11): the public URL https://<host>.<cluster>.devtunnels.ms
# /health was unhosted for 63 min and nothing watched it continuously -- only a periodic report
# eventually noticed, 15 min late. supervisor.ps1 watches the LOCAL side and the host-connection
# count on its own ~15 s loop, but a signed-out CLI makes it PAUSE tunnel management and write a
# single log line that nobody reads. This probe is the always-on PUBLIC-path check that was
# missing: it is separate from main.py (so it survives main.py death) and separate from the
# supervisor's pause (so a login lapse becomes a loud, named alert, not one quiet log line).
#
# THE VERDICT LOGIC IS NOT HERE. It lives in tools/tunnel_probe.py (pure, hermetic,
# CI-tested on Linux). This script only MEASURES (devtunnel user show, devtunnel show <name>,
# local /health, public /health) and hands the four observations to that module's `classify`.
# Keeping the decision in Python is deliberate: CI on this repo is Linux/pytest only, so the
# part that must never silently drift is the part that can actually be covered there.
#
# DEVICE-CODE IS NEVER USED. When sign-in is the cause, the alert tells the operator to run
# `devtunnel login` in a VISIBLE console -- this script does not launch any login itself, and
# it never passes `-d` / device-code anywhere (this tenant's Entra forbids device-code; a
# hidden launch silently falls back to it and is blocked). See docs/STARTUP_devtunnel_login.md.
#
# The PURE helpers at the top do no I/O and are unit-tested (public_url_probe.Tests.ps1 for the
# local Pester run; the classification itself is covered by tools/test_tunnel_probe_*.py in CI).
# The measurement loop at the bottom is behind a `$MyInvocation.InvocationName -ne '.'` guard so
# the test can dot-source this file without starting a probe.
#
# ASCII / ENGLISH ONLY (cmd/console safe). No BOM.

# --- PURE helpers (no network, no process I/O; unit-tested) ------------------------------------

function Get-AlertTitleForCause {
    # PURE. A short, cause-specific toast title. Unknown codes fall back to a generic title
    # rather than throwing -- a probe must not crash on an answer it did not expect.
    param([string]$Code)
    switch ($Code) {
        'not_logged_in'     { return 'Dev Tunnel: NOT SIGNED IN' }
        'no_host'           { return 'Dev Tunnel: NO HOST (0 connections)' }
        'local_server_down' { return 'MCP: LOCAL server down' }
        'public_path_down'  { return 'Dev Tunnel: PUBLIC path down' }
        default             { return 'Dev Tunnel: path unavailable' }
    }
}

function Get-AlertBodyForCause {
    # PURE. A self-contained, multi-line body: what failed, since when, and the exact next step.
    # The body must stand on its own (a toast click may do nothing if the AppId is unregistered),
    # so the operator can act from the text alone. For the sign-in cause it says explicitly to
    # use a VISIBLE console and NOT device-code.
    param(
        [string]$Code,
        [string]$Detail,
        [string]$PublicUrl,
        [int]$FailedForSec
    )
    $since = "failing for ~${FailedForSec}s"
    $base = "$Detail`n$since`npublic: $PublicUrl"
    switch ($Code) {
        'not_logged_in' {
            return ($base + "`n`nFIX: open a VISIBLE console and run:  devtunnel login" +
                    "`n(Do NOT use device-code / -d; this tenant's Entra forbids it and a hidden" +
                    "`nlaunch silently falls back to it. A login from a real console persists.)")
        }
        'no_host' {
            return ($base + "`n`nFIX: the supervisor re-hosts automatically once signed in." +
                    "`nIf it does not, check scripts/supervisor.ps1 is running and the tunnel name.")
        }
        'local_server_down' {
            return ($base + "`n`nFIX: the local MCP server is not answering on 127.0.0.1." +
                    "`nRestart it (start_all.bat) or check the supervisor log.")
        }
        'public_path_down' {
            return ($base + "`n`nFIX: local server and host are up, but the relay is not serving" +
                    "`nthis checkout (404/504, sign-in page, or a foreign pid). Check the relay/" +
                    "`ndevtunnel-host log; the supervisor re-hosts stale/ghost connections.")
        }
        default { return $base }
    }
}

function Test-ShouldAlertNow {
    # PURE. Decide whether THIS tick should raise a toast, given the current verdict and a tiny
    # bit of carried state. The design goal is 'visible alert within 60 s of failure' WITHOUT
    # a toast every 15 s for an hour:
    #   - healthy            -> never alert (and the caller resets state).
    #   - a NEW failure, or the cause CHANGED since the last alert -> alert at once.
    #   - the SAME failing cause as last time -> re-alert only after RepeatSec has elapsed.
    # With an interval <= 60 s this fires within one tick of a failure (well under 60 s) and then
    # repeats at a calm cadence. All time is passed in (NowSec) so this is pure and testable.
    param(
        [bool]$Healthy,
        [string]$Code,
        [string]$LastAlertedCode,
        [double]$LastAlertSec,
        [double]$NowSec,
        [double]$RepeatSec = 300
    )
    if ($Healthy) { return $false }
    if ($Code -ne $LastAlertedCode) { return $true }
    if ($LastAlertSec -le 0) { return $true }
    return (($NowSec - $LastAlertSec) -ge $RepeatSec)
}

# --- measurement (real I/O; skipped when dot-sourced) ------------------------------------------

function Invoke-PublicUrlProbe {
    param(
        [string]$Url = "",            # public /health URL; derived from the tunnel when absent
        [string]$TunnelName = "",
        [string]$LocalUrl = "",
        [int]$Port = 8000,
        [int]$IntervalSec = 15,      # <= 60 so a failure is alerted within 60 s
        [int]$RepeatSec = 300,       # re-alert cadence for an unchanged, still-failing cause
        [int]$HttpTimeoutSec = 4,    # local probe budget 2.0s + margin (matches 02 health doc)
        [switch]$Once,               # one tick then return (for a smoke run / the ELN)
        [string]$Log = ""
    )

    $root = Split-Path -Parent $PSScriptRoot
    $nameUtil = Join-Path $PSScriptRoot "tunnel_name_util.ps1"
    if (Test-Path $nameUtil) { . $nameUtil }

    if (-not $LocalUrl) { $LocalUrl = "http://127.0.0.1:$Port/health" }

    $dt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
    if (-not (Test-Path $dt)) { $dt = "devtunnel" }

    # Resolve tunnel name(s) once: explicit arg, else this machine's own default names.
    $names = @()
    if ($TunnelName) { $names = @($TunnelName) }
    elseif (Get-Command Get-OwnDefaultTunnelNames -ErrorAction SilentlyContinue) {
        $names = @(Get-OwnDefaultTunnelNames)
    }

    # Resolve the public /health URL (explicit -Url wins; else from `devtunnel show`).
    function Resolve-PublicHealth {
        if ($Url) {
            $u = $Url.Trim()
            if ($u -match '/health(\?|$)') { return $u }
            return ($u.TrimEnd('/') + "/health")
        }
        foreach ($n in $names) {
            if (-not $n) { continue }
            $show = @()
            try { $show = @(& $dt show $n 2>$null) } catch { $show = @() }
            $m = ($show | Select-String -Pattern 'https://[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.devtunnels\.ms\S*' -AllMatches |
                  ForEach-Object { $_.Matches } | ForEach-Object { $_.Value } | Select-Object -First 1)
            if ($m) { return ($m.TrimEnd('/') + "/health") }
        }
        return ""
    }

    function Test-HealthyBody {
        # Delegate the body rule to tools/tunnel_probe.py so the watcher and the CI-tested
        # module never disagree about what 'ready' means. Returns $true/$false.
        param([string]$Body)
        if (-not $Body) { return $false }
        $py = $null
        if (Get-Command Get-PythonForHelpers -ErrorAction SilentlyContinue) { $py = Get-PythonForHelpers }
        if (-not $py) { return $false }
        $probe = Join-Path $root "tools\tunnel_probe.py"
        if (-not (Test-Path $probe)) { return $false }
        $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Body))
        $code = "import base64,sys; sys.path.insert(0, r'$root'); from tools.tunnel_probe import is_healthy_mainpy_body as f; print('1' if f(base64.b64decode(sys.argv[1]).decode('utf-8')) else '0')"
        $pyExe = $py[0]; $pyArgs = @(); if ($py.Count -gt 1) { $pyArgs = @($py[1..($py.Count-1)]) }
        $out = ""
        try { $out = (& $pyExe @pyArgs -c $code $b64 2>$null | Select-Object -First 1) } catch { $out = "" }
        return ("$out".Trim() -eq '1')
    }

    function Get-Verdict {
        # Classify one tick by asking tools/tunnel_probe.py. Returns @{ code; healthy; detail }.
        param([string]$UserShow, [object]$HostConns, [bool]$LocalOk, [bool]$PublicOk)
        $py = $null
        if (Get-Command Get-PythonForHelpers -ErrorAction SilentlyContinue) { $py = Get-PythonForHelpers }
        if (-not $py) { return @{ code='local_server_down'; healthy=$false; detail='no python to classify' } }
        $probe = Join-Path $root "tools\tunnel_probe.py"
        if (-not (Test-Path $probe)) { return @{ code='local_server_down'; healthy=$false; detail='classifier missing' } }
        $hc = 'None'; if ($HostConns -ne $null) { $hc = [int]$HostConns }
        $u64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("$UserShow"))
        $lo = if ($LocalOk) { 'True' } else { 'False' }
        $po = if ($PublicOk) { 'True' } else { 'False' }
        $code = "import base64,sys,json; sys.path.insert(0, r'$root'); from tools.tunnel_probe import Observation, classify; u=base64.b64decode(sys.argv[1]).decode('utf-8'); hc=($hc); o=Observation(u, hc, $lo, $po); v=classify(o); print(json.dumps({'code':v.code,'healthy':v.healthy,'detail':v.detail}))"
        $pyExe = $py[0]; $pyArgs = @(); if ($py.Count -gt 1) { $pyArgs = @($py[1..($py.Count-1)]) }
        $out = ""
        try { $out = (& $pyExe @pyArgs -c $code $u64 2>$null | Select-Object -First 1) } catch { $out = "" }
        if (-not $out) { return @{ code='local_server_down'; healthy=$false; detail='classifier gave no answer' } }
        try { $j = $out | ConvertFrom-Json } catch { return @{ code='local_server_down'; healthy=$false; detail='bad classifier output' } }
        return @{ code=$j.code; healthy=[bool]$j.healthy; detail="$($j.detail)" }
    }

    function Get-Http {
        param([string]$HealthUrl)
        if (-not $HealthUrl) { return "" }
        try {
            $r = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec $HttpTimeoutSec -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
            if ($r.StatusCode -eq 200) { return [string]$r.Content }
            return ""
        } catch { return "" }
    }

    function Say([string]$m) {
        $line = "{0} {1}" -f ((Get-Date).ToUniversalTime().ToString("MM-dd HH:mm:ss")), $m
        Write-Host $line
        if ($Log) { try { Add-Content -Path $Log -Value $line -Encoding utf8 } catch { } }
    }

    $lastAlertedCode = ""
    $lastAlertSec = 0.0
    $firstFailSec = 0.0

    Say ("public_url_probe started; interval=${IntervalSec}s repeat=${RepeatSec}s")

    while ($true) {
        $nowSec = ([DateTimeOffset](Get-Date)).ToUnixTimeMilliseconds() / 1000.0
        $publicHealth = Resolve-PublicHealth

        # devtunnel user show -> signed in?
        $userShow = ""
        try { $userShow = (@(& $dt user show 2>$null) -join "`n") } catch { $userShow = "" }

        # host connections for the first resolvable name
        $hostConns = $null
        foreach ($n in $names) {
            if (-not $n) { continue }
            $show = @()
            try { $show = @(& $dt show $n 2>$null) } catch { $show = @() }
            if (Get-Command Get-HostConnectionsFromShow -ErrorAction SilentlyContinue) {
                $hc = Get-HostConnectionsFromShow $show
                if ($hc -ne $null) { $hostConns = $hc; break }
            }
        }

        $localOk  = Test-HealthyBody (Get-Http $LocalUrl)
        $publicOk = Test-HealthyBody (Get-Http $publicHealth)

        $v = Get-Verdict -UserShow $userShow -HostConns $hostConns -LocalOk $localOk -PublicOk $publicOk

        if ($v.healthy) {
            if ($firstFailSec -gt 0) { Say ("recovered: " + $v.detail) }
            $firstFailSec = 0.0; $lastAlertedCode = ""; $lastAlertSec = 0.0
        } else {
            if ($firstFailSec -le 0) { $firstFailSec = $nowSec }
            $failedFor = [int]($nowSec - $firstFailSec)
            $shouldAlert = Test-ShouldAlertNow -Healthy $false -Code $v.code `
                -LastAlertedCode $lastAlertedCode -LastAlertSec $lastAlertSec -NowSec $nowSec -RepeatSec $RepeatSec
            Say ("FAIL[" + $v.code + "] " + $v.detail + "  (failedFor=${failedFor}s alert=$shouldAlert)")
            if ($shouldAlert) {
                $title = Get-AlertTitleForCause $v.code
                $body  = Get-AlertBodyForCause -Code $v.code -Detail $v.detail -PublicUrl $publicHealth -FailedForSec $failedFor
                $py = $null
                if (Get-Command Get-PythonForHelpers -ErrorAction SilentlyContinue) { $py = Get-PythonForHelpers }
                if ($py) {
                    $b64t = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($title))
                    $b64b = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($body))
                    $logDir = Join-Path $root "logs"
                    $launch = "file:///" + ($logDir -replace '\\','/')
                    $b64l = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($launch))
                    $nc = "import base64,sys; sys.path.insert(0, r'$root'); from tools.notify_ops import notify_desktop as n; print(n(base64.b64decode(sys.argv[1]).decode('utf-8'), base64.b64decode(sys.argv[2]).decode('utf-8'), launch=base64.b64decode(sys.argv[3]).decode('utf-8')))"
                    $pyExe = $py[0]; $pyArgs = @(); if ($py.Count -gt 1) { $pyArgs = @($py[1..($py.Count-1)]) }
                    try { & $pyExe @pyArgs -c $nc $b64t $b64b $b64l 2>$null | Out-Null } catch { }
                }
                $lastAlertedCode = $v.code; $lastAlertSec = $nowSec
            }
        }

        if ($Once) { return $v }
        Start-Sleep -Seconds $IntervalSec
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $bound = @{}
    for ($i = 0; $i -lt $args.Count; $i++) {
        $a = "$($args[$i])"
        if ($a -like '-*') {
            $key = $a.TrimStart('-')
            if (($i + 1) -lt $args.Count -and "$($args[$i+1])" -notlike '-*') {
                $bound[$key] = $args[$i+1]; $i++
            } else { $bound[$key] = $true }
        }
    }
    Invoke-PublicUrlProbe @bound
}
