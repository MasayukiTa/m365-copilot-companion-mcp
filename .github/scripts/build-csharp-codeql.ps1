param()

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$out = Join-Path $repo ".codeql-build\csharp"
$ui = Join-Path $repo "ui"
$thirdparty = Join-Path $repo "thirdparty"
$fw = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
$csc = Join-Path $fw "csc.exe"
$wpf = Join-Path $fw "WPF"

if (-not (Test-Path $csc)) {
    throw "csc.exe not found at $csc; .NET Framework 4.x is required for the WPF UI build."
}

New-Item -ItemType Directory -Force -Path $out | Out-Null

function Invoke-Csc {
    param(
        [string] $Name,
        [string[]] $Sources,
        [string[]] $References,
        [string] $Target = "winexe",
        [string[]] $ExtraArgs = @()
    )

    $exe = Join-Path $out ($Name + ".exe")
    $args = @("/nologo", "/target:$Target", "/out:$exe") + $ExtraArgs
    foreach ($ref in $References) {
        $args += "/r:$ref"
    }
    $args += $Sources

    Write-Host "Building $Name for CodeQL..."
    & $csc @args
    if ($LASTEXITCODE -ne 0) {
        throw "C# build failed: $Name"
    }
}

# EVERY reference either UI binary needs, given to both. The per-target split that used to live
# here (Forms for the cockpit only) was a second place to keep a fact, and an unused /r: costs
# csc nothing. CodeQL wants the sources compiled, not a minimal reference set.
$wpfRefs = @(
    (Join-Path $wpf "PresentationFramework.dll"),
    (Join-Path $wpf "PresentationCore.dll"),
    (Join-Path $wpf "WindowsBase.dll"),
    (Join-Path $fw "System.Xaml.dll"),
    (Join-Path $fw "System.Web.Extensions.dll"),
    (Join-Path $fw "System.Windows.Forms.dll"),
    (Join-Path $fw "System.IO.Compression.dll")
)

$manifest = Join-Path $ui "app.manifest"
$manifestArgs = @()
if (Test-Path $manifest) {
    $manifestArgs = @("/win32manifest:$manifest")
}

function Get-UiTargets {
    <#
      READ THE REAL BUILD'S LIST; DO NOT COPY IT.

      This file used to restate which .cs go into each binary, and the copy went stale the first
      time a shared source was added: ui/FleetCommands.cs went into rebuild_ui.ps1's two Build
      lines and nowhere else, and this step failed on main with
      "CS0103: The name 'FleetCommands' does not exist in the current context" -- after a local
      run that was green, because nothing local compiles C#.

      bench/ui_build_check.py had already been fixed exactly this way, with the reason written
      out: "READ FROM THE REAL BUILD, NOT COPIED FROM IT ... The same omission-by-hand has
      broken this project's UI before." The answer was in the repository; this file was not
      using it. ui/rebuild_ui.ps1 produces the shipped binaries, so its Build lines are the one
      definition.

      An unreadable list is a HARD FAILURE, never an empty one: compiling nothing would report
      a clean CodeQL result for a UI that was never looked at.
    #>
    $path = Join-Path $ui "rebuild_ui.ps1"
    $targets = @()
    foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        $m = [regex]::Match($line, '^\s*Build\s+"([A-Za-z0-9_]+)"\s+@\((.*)\)\s*$')
        if ($m.Success) {
            $srcs = @()
            foreach ($s in [regex]::Matches($m.Groups[2].Value, '"([^"]+\.cs)"')) {
                $srcs += $s.Groups[1].Value
            }
            $targets += , @($m.Groups[1].Value, $srcs)
        }
    }
    if ($targets.Count -eq 0) {
        throw "no Build lines found in $path -- refusing to compile a source list I cannot read, because passing here would mean nothing"
    }
    return $targets
}

foreach ($t in (Get-UiTargets)) {
    $name = $t[0]
    $sources = @()
    foreach ($s in $t[1]) {
        $sources += (Join-Path $ui $s)
    }
    Write-Host ("  {0} <- {1}" -f $name, ($t[1] -join ", "))
    Invoke-Csc `
        -Name $name `
        -References $wpfRefs `
        -ExtraArgs $manifestArgs `
        -Sources $sources
}

Invoke-Csc `
    -Name "VirtualDesktop11" `
    -Target "exe" `
    -References @() `
    -Sources @(
        (Join-Path $thirdparty "VirtualDesktop11.cs")
    )

Write-Host "C# CodeQL build complete."
