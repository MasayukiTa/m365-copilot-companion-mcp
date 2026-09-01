# Export this machine's trusted root certificates to a PEM bundle.
#
# WHY. A TLS-intercepting proxy installs its own root CA into the WINDOWS certificate store.
# Anything that reads that store works; anything carrying its own bundled roots does not. uv is
# the second kind -- a Rust binary using rustls with webpki-roots -- so on a fresh machine
# behind such a proxy it fails every download with:
#
#     Caused by: invalid peer certificate: UnknownIssuer
#
# The tell is that PowerShell's own Invoke-RestMethod SUCCEEDS on the same network in the same
# script (it uses the Windows store), so uv installs fine and then cannot fetch a Python.
#
# WHY EXPORT RATHER THAN DISABLE VERIFICATION. pip is already worked around here with
# --trusted-host, which turns certificate checking OFF for those hosts. That is a reasonable
# expedient and it stays, but it is not what should be reached for first: exporting the roots
# the machine already trusts keeps verification ON and works for every tool that reads a PEM,
# not just pip. The corporate CA is a legitimate trust anchor on this machine; the problem was
# only ever that some tools could not see it.
#
# DISCOVERED, NOT CONFIGURED. Nothing here names a company, a proxy, or a certificate. It reads
# whatever this machine trusts, so it works on a machine with no interception too (it just
# exports the public roots and changes nothing).

[CmdletBinding()]
param(
    [string]$OutFile = ".setup\ca-bundle.pem",
    # THE MANUAL ESCAPE HATCH. Reading the Windows stores covers the normal case -- a corporate
    # root deployed by Group Policy to LocalMachine\Root, readable by any user without admin --
    # but it is not guaranteed. A machine that is not domain-joined, or a CA distributed some
    # other way, can leave nothing to find. An operator who has the .cer/.pem in hand drops it
    # at .setup\ca-extra.pem (or points -ExtraPem at it) and it is appended to the bundle.
    [string]$ExtraPem = ".setup\ca-extra.pem",
    # Refuse to reuse a bundle older than this; roots do change, and a stale bundle fails in a
    # way that looks exactly like the problem it was meant to solve.
    [int]$MaxAgeHours = 24
)

$ErrorActionPreference = 'Stop'

function Write-Pem {
    param($Cert, $Writer)
    $b64 = [Convert]::ToBase64String($Cert.RawData, 'InsertLineBreaks')
    $Writer.WriteLine("# Subject: " + $Cert.Subject)
    $Writer.WriteLine("-----BEGIN CERTIFICATE-----")
    $Writer.WriteLine($b64)
    $Writer.WriteLine("-----END CERTIFICATE-----")
}

$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }

if (Test-Path $OutFile) {
    $age = (Get-Date) - (Get-Item $OutFile).LastWriteTime
    if ($age.TotalHours -lt $MaxAgeHours -and (Get-Item $OutFile).Length -gt 1000) {
        Write-Output $OutFile
        exit 0
    }
}

# BOTH ROOT AND INTERMEDIATE, and both scopes. An intercepting proxy's chain is commonly a
# root in LocalMachine\Root with an issuing intermediate in LocalMachine\CA; exporting only the
# roots produces a bundle that still cannot complete the chain, which fails identically to
# having no bundle at all and is far harder to diagnose the second time.
$stores = @(
    'Cert:\LocalMachine\Root',
    'Cert:\LocalMachine\CA',
    'Cert:\CurrentUser\Root',
    'Cert:\CurrentUser\CA'
)

$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$tmp = "$OutFile.tmp"
$sw = New-Object System.IO.StreamWriter($tmp, $false, (New-Object System.Text.UTF8Encoding($false)))
$count = 0
try {
    foreach ($store in $stores) {
        if (-not (Test-Path $store)) { continue }
        foreach ($c in (Get-ChildItem $store -ErrorAction SilentlyContinue)) {
            if (-not $c.Thumbprint) { continue }
            if (-not $seen.Add($c.Thumbprint)) { continue }
            # An expired root cannot validate anything and only makes the bundle bigger.
            if ($c.NotAfter -lt (Get-Date)) { continue }
            Write-Pem -Cert $c -Writer $sw
            $count++
        }
    }
} finally {
    $sw.Close()
}

# The operator's own certificate(s), appended after the machine's. Accepts a PEM, or a DER
# .cer which is converted -- handing someone a file and telling them it is the wrong encoding
# is not help.
$extraCount = 0
if ($ExtraPem -and (Test-Path $ExtraPem)) {
    try {
        $raw = [System.IO.File]::ReadAllBytes($ExtraPem)
        $text = [System.Text.Encoding]::ASCII.GetString($raw)
        $sw2 = New-Object System.IO.StreamWriter($tmp, $true, (New-Object System.Text.UTF8Encoding($false)))
        try {
            if ($text -match '-----BEGIN CERTIFICATE-----') {
                $sw2.WriteLine("# operator-supplied: $ExtraPem")
                $sw2.WriteLine($text.Trim())
                $extraCount = ([regex]::Matches($text, '-----BEGIN CERTIFICATE-----')).Count
            } else {
                $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,$raw)
                $sw2.WriteLine("# operator-supplied (DER): $ExtraPem")
                $sw2.WriteLine("-----BEGIN CERTIFICATE-----")
                $sw2.WriteLine([Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'))
                $sw2.WriteLine("-----END CERTIFICATE-----")
                $extraCount = 1
            }
        } finally { $sw2.Close() }
        $count += $extraCount
    } catch {
        Write-Warning "could not read $ExtraPem : $($_.Exception.Message)"
    }
}

if ($count -lt 5) {
    # FAIL LOUDLY RATHER THAN WRITE A USELESS BUNDLE. A near-empty PEM would be accepted by
    # every consumer and then reject every certificate, which reads as a network fault.
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Error "only $count certificate(s) could be read from the Windows stores; refusing to write a bundle that would reject everything"
    exit 1
}

Move-Item -Force $tmp $OutFile

# Say what was found on stderr, so the operator can tell "exported the public roots and your
# proxy CA is not among them" from "exported everything". Silence here is how someone spends an
# hour on a bundle that was never going to contain what they needed.
$machineIssued = 0
foreach ($store in @('Cert:\LocalMachine\CA', 'Cert:\CurrentUser\CA')) {
    if (Test-Path $store) {
        $machineIssued += @(Get-ChildItem $store -ErrorAction SilentlyContinue).Count
    }
}
Write-Host "ca_bundle: $count certificate(s) exported ($extraCount operator-supplied, $machineIssued intermediate CA entries present)"
if ($extraCount -eq 0 -and $machineIssued -eq 0) {
    Write-Host "ca_bundle: no intermediate CA entries found. If TLS still fails, this machine may"
    Write-Host "ca_bundle: not carry the intercepting CA -- put it at $ExtraPem and re-run."
}
Write-Output (Resolve-Path $OutFile).Path
