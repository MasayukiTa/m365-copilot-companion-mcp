param(
    [string]$Version = "ci"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$dist = Join-Path $repo "dist"
$zipName = "M365-Companion-$Version.zip"
$zipPath = Join-Path $dist $zipName
$shaPath = Join-Path $dist "SHA256SUMS.txt"

& (Join-Path $repo "scripts\package_release.ps1") -Version $Version -OutDir "dist"
if ($LASTEXITCODE -ne 0) {
    throw "package_release.ps1 failed"
}

if (-not (Test-Path $zipPath)) {
    throw "release ZIP was not created: $zipPath"
}
if (-not (Test-Path $shaPath)) {
    throw "SHA256SUMS.txt was not created"
}

$expectedHash = (Get-Content -Path $shaPath -Raw).Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)[0]
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "SHA256 mismatch: expected $expectedHash actual $actualHash"
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($zip.Entries | ForEach-Object { $_.FullName })
} finally {
    $zip.Dispose()
}

$root = "M365-Companion-$Version/"
$required = @(
    "${root}quickstart.bat",
    "${root}update.bat",
    "${root}scripts/update_from_release.py",
    "${root}scripts/package_release.ps1",
    "${root}main.py",
    "${root}requirements.txt",
    "${root}.release_info.json",
    "${root}.release_manifest.json"
)

foreach ($entry in $required) {
    if ($entries -notcontains $entry) {
        throw "release ZIP is missing required entry: $entry"
    }
}

$forbiddenPrefixes = @(
    "${root}.git/",
    "${root}.github/",
    "${root}.venv/",
    "${root}.fleet/",
    "${root}.setup/"
)
$forbiddenExact = @(
    "${root}.env"
)

foreach ($entry in $entries) {
    if ($forbiddenExact -contains $entry) {
        throw "release ZIP includes forbidden entry: $entry"
    }
    foreach ($prefix in $forbiddenPrefixes) {
        if ($entry.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "release ZIP includes forbidden prefix: $entry"
        }
    }
}

Write-Host "Release package smoke test passed: $zipName"
