<#
.SYNOPSIS
  Build the install ZIP attached to GitHub Releases.

.DESCRIPTION
  Creates a curated ZIP from committed files at HEAD. It intentionally excludes
  runtime state, .venv, pip-installed packages, logs, caches, CI metadata, bench
  harnesses, and local untracked files. The ZIP is an install snapshot: unzip it
  and run quickstart.bat. For incremental git updates, install with git clone.

  ASCII / ENGLISH ONLY. Do not add non-ASCII text to this script.
#>
param(
    [string]$Version,
    [string]$OutDir = "dist"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repoRoot

if (-not $Version) {
    $tag = (& git describe --tags --exact-match 2>$null)
    if ($LASTEXITCODE -eq 0 -and $tag) {
        $Version = $tag.Trim()
    } else {
        $short = (& git rev-parse --short HEAD).Trim()
        $Version = "snapshot-$short"
    }
}

$packageName = "M365-Companion-$Version"
$outPath = Join-Path $repoRoot $OutDir
New-Item -ItemType Directory -Force -Path $outPath | Out-Null

$zipPath = Join-Path $outPath "$packageName.zip"
$shaPath = Join-Path $outPath "SHA256SUMS.txt"
if (Test-Path $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
if (Test-Path $shaPath) { Remove-Item -LiteralPath $shaPath -Force }

$paths = @(
    ".env.example",
    ".gitignore",
    "LICENSE",
    "README.md",
    "configure_env.bat",
    "copilot_studio_values.bat",
    "doctor.bat",
    "quickstart.bat",
    "rotate_secrets.bat",
    "setup.bat",
    "start_all.bat",
    "update.bat",
    "main.py",
    "requirements.txt",
    "requirements-relay.txt",
    "agent_memory",
    "bridge",
    "docs",
    "generated",
    "relay",
    "scripts",
    "thirdparty/VirtualDesktop11.cs",
    "tools",
    "ui"
)

Write-Host "Building release ZIP from committed HEAD:"
Write-Host "  package: $packageName"
Write-Host "  output:  $zipPath"

& git archive --format=zip --prefix="$packageName/" --output="$zipPath" HEAD -- $paths
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

$commit = (& git rev-parse HEAD).Trim()
$releaseInfo = @{
    tag_name = $Version
    commit = $commit
    package = "$packageName.zip"
} | ConvertTo-Json -Compress

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $entry = $zip.CreateEntry("$packageName/.release_info.json")
    $writer = New-Object System.IO.StreamWriter($entry.Open(), [System.Text.Encoding]::UTF8)
    try { $writer.WriteLine($releaseInfo) } finally { $writer.Dispose() }

    $sha = [System.Security.Cryptography.SHA256]::Create()
    $files = [ordered]@{}
    foreach ($e in @($zip.Entries)) {
        if ($e.FullName.EndsWith("/")) { continue }
        $rel = $e.FullName
        if ($rel.StartsWith("$packageName/")) { $rel = $rel.Substring($packageName.Length + 1) }
        if ($rel -eq ".release_manifest.json") { continue }
        $stream = $e.Open()
        try {
            $bytes = $sha.ComputeHash($stream)
            $files[$rel] = ([BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant())
        } finally {
            $stream.Dispose()
        }
    }
    $manifest = @{
        tag_name = $Version
        commit = $commit
        files = $files
    } | ConvertTo-Json -Depth 5

    $manifestEntry = $zip.CreateEntry("$packageName/.release_manifest.json")
    $manifestWriter = New-Object System.IO.StreamWriter($manifestEntry.Open(), [System.Text.Encoding]::UTF8)
    try { $manifestWriter.WriteLine($manifest) } finally { $manifestWriter.Dispose() }
} finally {
    $zip.Dispose()
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
Set-Content -Path $shaPath -Encoding ASCII -Value "$hash  $(Split-Path -Leaf $zipPath)"

Write-Host "Wrote:"
Write-Host "  $zipPath"
Write-Host "  $shaPath"
