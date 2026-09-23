# detect_proxy.ps1 -- print the proxy URL this PC's own settings say to use for HTTPS, or
# nothing when it connects directly.
#
# WHY (D10 in the new-PC install review, 2026-09-24). uv is a Rust binary and git and pip read
# proxies from the environment (HTTPS_PROXY); none of them look at the proxy Windows is
# configured with (Internet Options / a PAC script / `netsh winhttp`). On a network with an
# EXPLICIT proxy the browser works, PowerShell works (it uses the system proxy), and uv, pip and
# git fail -- after which setup printed certificate advice, sending the reader to the wrong fix.
# Nothing derived a proxy anywhere (git grep: only setup_devtunnel.ps1 used DefaultWebProxy).
#
# Output: one line, e.g.  http://proxy.example:8080   -- or no output at all.
# Sources, in order: WinINET static proxy (Internet Options, per user), WinINET PAC script
# (AutoConfigURL, resolved for https://pypi.org/), WinHTTP (`netsh winhttp set proxy`, read from
# the registry because netsh's own output is localized). The caller only runs this when
# HTTPS_PROXY is not already set, so an explicit setting always wins.
#
# A proxy that needs a password cannot be expressed here without writing the password down;
# setup's failure message says what to do in that case.
#
# ASCII / ENGLISH ONLY.

function ConvertTo-ProxyUrl {
    # "host:port" | "http=h:p;https=h2:p2" | "http://h:p" -> "http://h:p" (or '' when unusable).
    param([string]$Server, [string]$Scheme = 'https')
    $s = ([string]$Server).Trim()
    if (-not $s) { return '' }
    if ($s -match '=') {
        $map = @{}
        foreach ($part in ($s -split ';')) {
            if ($part -match '^\s*([A-Za-z]+)\s*=\s*(\S+)\s*$') { $map[$Matches[1].ToLower()] = $Matches[2] }
        }
        $pick = $map[$Scheme]
        if (-not $pick) { $pick = $map['http'] }
        # socks= alone is deliberately not used: pip cannot speak SOCKS without an extra package.
        if (-not $pick) { return '' }
        $s = $pick
    } else {
        $s = ($s -split ';')[0].Trim()
    }
    if (-not $s) { return '' }
    if ($s -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $s = 'http://' + $s }
    return $s.TrimEnd('/')
}

function Get-WinHttpProxyFromBytes {
    # HKLM\...\Internet Settings\Connections\WinHttpSettings: DWORD version, DWORD counter,
    # DWORD flags (bit 2 = a proxy is set), DWORD length, then the proxy string (ASCII).
    param($Bytes)
    if (-not $Bytes -or $Bytes.Count -lt 16) { return '' }
    $flags = [int]$Bytes[8] + 256 * [int]$Bytes[9]
    if (($flags -band 2) -eq 0) { return '' }
    $len = [int]$Bytes[12] + 256 * [int]$Bytes[13] + 65536 * [int]$Bytes[14]
    if ($len -le 0 -or (16 + $len) -gt $Bytes.Count) { return '' }
    return (-join ($Bytes[16..(15 + $len)] | ForEach-Object { [char][int]$_ }))
}

function Select-SystemProxy {
    # PURE: the decision over already-read values, so it is testable without this PC's settings.
    param([string]$WinInetEnable, [string]$WinInetServer, [string]$PacProxy, [string]$WinHttpServer)
    if (([string]$WinInetEnable).Trim() -eq '1') {
        $u = ConvertTo-ProxyUrl $WinInetServer
        if ($u) { return $u }
    }
    $u = ConvertTo-ProxyUrl $PacProxy
    if ($u) { return $u }
    return (ConvertTo-ProxyUrl $WinHttpServer)
}

if (-not $env:DETECT_PROXY_NO_AUTORUN) {
    $inet = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    $enable = ''; $server = ''; $pac = ''
    if ($inet) { $enable = [string]$inet.ProxyEnable; $server = [string]$inet.ProxyServer }
    if ($inet -and $inet.AutoConfigURL) {
        # Only when a PAC script is configured: GetSystemWebProxy can otherwise spend seconds on
        # WPAD discovery for a machine that has no proxy at all.
        try {
            $target = New-Object System.Uri 'https://pypi.org/simple/'
            $p = [System.Net.WebRequest]::GetSystemWebProxy().GetProxy($target)
            if ($p -and $p.Host -ne $target.Host) { $pac = $p.Scheme + '://' + $p.Authority }
        } catch { $pac = '' }
    }
    $wh = ''
    try {
        $raw = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\Connections' -Name WinHttpSettings -ErrorAction Stop).WinHttpSettings
        $wh = Get-WinHttpProxyFromBytes $raw
    } catch { $wh = '' }
    $url = Select-SystemProxy -WinInetEnable $enable -WinInetServer $server -PacProxy $pac -WinHttpServer $wh
    if ($url) { Write-Output $url }
    exit 0
}
