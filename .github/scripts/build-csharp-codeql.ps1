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

$wpfRefs = @(
    (Join-Path $wpf "PresentationFramework.dll"),
    (Join-Path $wpf "PresentationCore.dll"),
    (Join-Path $wpf "WindowsBase.dll"),
    (Join-Path $fw "System.Xaml.dll"),
    (Join-Path $fw "System.Web.Extensions.dll")
)

$manifest = Join-Path $ui "app.manifest"
$manifestArgs = @()
if (Test-Path $manifest) {
    $manifestArgs = @("/win32manifest:$manifest")
}

Invoke-Csc `
    -Name "CopilotChat" `
    -References $wpfRefs `
    -ExtraArgs $manifestArgs `
    -Sources @(
        (Join-Path $ui "CopilotChat.cs"),
        (Join-Path $ui "Markdown.cs"),
        (Join-Path $ui "Theme.cs")
    )

Invoke-Csc `
    -Name "FleetCockpit" `
    -References ($wpfRefs + (Join-Path $fw "System.Windows.Forms.dll")) `
    -ExtraArgs $manifestArgs `
    -Sources @(
        (Join-Path $ui "FleetCockpit.cs"),
        (Join-Path $ui "SelfImproveDashboard.cs"),
        (Join-Path $ui "Theme.cs")
    )

Invoke-Csc `
    -Name "VirtualDesktop11" `
    -Target "exe" `
    -References @() `
    -Sources @(
        (Join-Path $thirdparty "VirtualDesktop11.cs")
    )

Write-Host "C# CodeQL build complete."
