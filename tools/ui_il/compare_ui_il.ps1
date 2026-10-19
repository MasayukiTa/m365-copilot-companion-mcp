# compare_ui_il.ps1 -- say whether two versions of the WPF UI sources compile to the SAME program.
#
# WHY. ui/CopilotChat.cs and ui/SelfImproveDashboard.cs are being split into partial-class files
# by moving code only. "Nothing changed" cannot be seen by diffing the .exe files (every csc run
# embeds a fresh GUID, so two builds of identical source differ byte for byte). This script builds
# a BEFORE tree and an AFTER tree and compares the IL of every method, after normalising the
# compiler-generated names (tools/ui_il/ilcanon.cs). A pure move prints MATCH; a changed method,
# field or type prints one DIFF line per difference.
#
# USE (from any directory; nothing is written under the repository's ui\ folder):
#   .\tools\ui_il\compare_ui_il.ps1 -Before <commit-ish or ui-dir> -After <commit-ish or ui-dir>
#   .\tools\ui_il\compare_ui_il.ps1 -Before HEAD~1 -After HEAD -Targets CopilotChat
#   .\tools\ui_il\compare_ui_il.ps1 -Before HEAD -After C:\scratch\copy\ui -Keep
#
#   -Before / -After  A git revision of the repository that holds this script (the ui\ folder of
#                     that commit is extracted with `git archive`), or the path of a folder that
#                     IS a ui\ folder (it must contain rebuild_ui.ps1), or a repository root
#                     (its ui\ subfolder is used).
#   -Targets          Which executables to compare. Default: FleetCockpit and CopilotChat.
#   -Scratch          Parent folder for the build output. Default: a new folder under $env:TEMP.
#                     Refused when it is inside this repository's ui\ folder.
#   -Keep             Keep the scratch folder (the two IL dumps are there for a closer look).
#
# HOW IT BUILDS. For each tree it reads the `Build "<name>" @(...)` lines of THAT tree's own
# ui\rebuild_ui.ps1 (the one list of what is compiled) and calls the same csc.exe with the same
# references, /target:winexe and manifest rule as rebuild_ui.ps1's Build function, but with /out
# in the scratch folder. It never runs rebuild_ui.ps1, never touches ui\*.exe, never starts or
# stops a process other than csc.exe and the small ilcanon.exe it builds itself.
#
# OUTPUT. "MATCH <target>" per target when the normalised IL is identical, otherwise
# "DIFF <target>: <what differs>" lines. Exit code 0 when every target matches, 1 when any
# differs, 2 when a build or the tooling failed.
#
# KNOWN LIMIT. Initialised fields moved to another file change the order of assignments in
# .ctor/.cctor: that shows up as a DIFF of exactly those methods, which is the point
# (docs: partial-class field initialisers run in compiler-argument order).
param(
    [Parameter(Mandatory = $true)][string]$Before,
    [Parameter(Mandatory = $true)][string]$After,
    [string[]]$Targets = @("FleetCockpit", "CopilotChat"),
    [string]$Scratch = "",
    [switch]$Keep
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (& git -C $here rev-parse --show-toplevel).Trim()
$repoUi = [System.IO.Path]::GetFullPath((Join-Path $repo "ui"))
$FW  = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
$CSC = "$FW\csc.exe"
$WPF = "$FW\WPF"
if (-not (Test-Path $CSC)) { Write-Host "ERROR: csc.exe not found ($CSC)"; exit 2 }

if ($Scratch -eq "") { $Scratch = Join-Path $env:TEMP ("ui_il_" + [guid]::NewGuid().ToString("N").Substring(0, 8)) }
$Scratch = [System.IO.Path]::GetFullPath($Scratch)
if ($Scratch.StartsWith($repoUi, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "ERROR: -Scratch must not be inside the repository's ui folder ($repoUi)"; exit 2
}
New-Item -ItemType Directory -Force -Path $Scratch | Out-Null

# Resolve one side to a ui folder: a directory as given, or `git archive <rev> ui` extracted.
function Resolve-UiTree($spec, $label) {
    if (Test-Path -LiteralPath $spec -PathType Container) {
        $full = [System.IO.Path]::GetFullPath($spec)
        if (Test-Path (Join-Path $full "rebuild_ui.ps1")) { return $full }
        if (Test-Path (Join-Path $full "ui\rebuild_ui.ps1")) { return (Join-Path $full "ui") }
        Write-Host ("ERROR: " + $label + ": " + $full + " has no rebuild_ui.ps1"); exit 2
    }
    $dest = Join-Path $Scratch ($label + "_src")
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $tar = Join-Path $Scratch ($label + ".tar")
    & git -C $repo archive --format=tar -o $tar $spec ui
    if ($LASTEXITCODE -ne 0) { Write-Host ("ERROR: git archive failed for " + $label + " = " + $spec); exit 2 }
    & tar -xf $tar -C $dest
    if ($LASTEXITCODE -ne 0) { Write-Host ("ERROR: tar failed for " + $label); exit 2 }
    return (Join-Path $dest "ui")
}

# The source list of one target, read from that tree's own rebuild_ui.ps1 Build line.
function Get-BuildSources($uiDir, $name) {
    $line = Get-Content -LiteralPath (Join-Path $uiDir "rebuild_ui.ps1") |
        Where-Object { ($_ -match '^Build\s+"([A-Za-z0-9_]+)"\s+@\(') -and ($Matches[1] -eq $name) } | Select-Object -First 1
    if (-not $line) { Write-Host ("ERROR: no Build `"" + $name + "`" line in " + $uiDir + "\rebuild_ui.ps1"); exit 2 }
    return @([regex]::Matches($line, '"([^"]+\.cs)"') | ForEach-Object { $_.Groups[1].Value })
}

# Same csc call as ui/rebuild_ui.ps1's Build function, with /out in the scratch folder.
function Build-Target($uiDir, $name, $outDir) {
    $sources = Get-BuildSources $uiDir $name
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    $out = Join-Path $outDir ($name + ".exe")
    $refs = @("/r:$WPF\PresentationFramework.dll", "/r:$WPF\PresentationCore.dll", "/r:$WPF\WindowsBase.dll",
              "/r:$FW\System.Xaml.dll", "/r:$FW\System.Web.Extensions.dll")
    if ($name -eq "FleetCockpit") { $refs += "/r:$FW\System.Windows.Forms.dll" }
    if ($name -eq "CopilotChat") { $refs += "/r:$FW\System.IO.Compression.dll" }
    $manifest = if (Test-Path (Join-Path $uiDir "app.manifest")) { @("/win32manifest:" + (Join-Path $uiDir "app.manifest")) } else { @() }
    $cscArgs = @("/nologo", "/target:winexe", "/out:$out") + $manifest + $refs + ($sources | ForEach-Object { Join-Path $uiDir $_ })
    $log = & $CSC @cscArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("BUILD FAILED: " + $name + " in " + $uiDir)
        $log | Select-Object -Last 15 | ForEach-Object { Write-Host $_ }
        exit 2
    }
    return $out
}

# The IL dumper, built from the source next to this script.
$ilcanon = Join-Path $Scratch "ilcanon.exe"
$log = & $CSC "/nologo" "/target:exe" "/out:$ilcanon" (Join-Path $here "ilcanon.cs") 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED: ilcanon"; $log | ForEach-Object { Write-Host $_ }; exit 2 }

$beforeUi = Resolve-UiTree $Before "before"
$afterUi = Resolve-UiTree $After "after"
Write-Host ("before = " + $Before + "  (" + $beforeUi + ")")
Write-Host ("after  = " + $After + "  (" + $afterUi + ")")

$rc = 0
foreach ($t in $Targets) {
    $b = Build-Target $beforeUi $t (Join-Path $Scratch "before_out")
    $a = Build-Target $afterUi $t (Join-Path $Scratch "after_out")
    $bTxt = Join-Path $Scratch ("before_" + $t + ".txt")
    $aTxt = Join-Path $Scratch ("after_" + $t + ".txt")
    $null = & $ilcanon $b $bTxt
    if ($LASTEXITCODE -ne 0) { Write-Host ("ERROR: ilcanon failed on " + $b); exit 2 }
    $null = & $ilcanon $a $aTxt
    if ($LASTEXITCODE -ne 0) { Write-Host ("ERROR: ilcanon failed on " + $a); exit 2 }
    $hb = (Get-FileHash -Algorithm SHA256 $bTxt).Hash
    $ha = (Get-FileHash -Algorithm SHA256 $aTxt).Hash
    $n = @(Get-Content -LiteralPath $bTxt).Count
    if ($hb -eq $ha) {
        Write-Host ("MATCH " + $t + "  (" + $n + " lines of normalised IL, sha256 " + $hb.Substring(0, 16) + ")")
        continue
    }
    $rc = 1
    # A line is "METHOD <signature> | <body>", "FIELD ..." or "TYPE ...": name the thing that differs.
    # -CaseSensitive: a string literal that differs only in case ("not" -> "NOT") is a real change.
    $diff = Compare-Object -CaseSensitive @(Get-Content -LiteralPath $bTxt) @(Get-Content -LiteralPath $aTxt)
    $names = $diff | ForEach-Object {
        $side = if ($_.SideIndicator -eq "<=") { "only-before" } else { "only-after" }
        $key = $_.InputObject
        $bar = $key.IndexOf(" | ")
        if ($bar -gt 0) { $key = $key.Substring(0, $bar) } elseif ($key.Length -gt 200) { $key = $key.Substring(0, 200) }
        ($key + "   [" + $side + "]")
    } | Sort-Object -Unique
    Write-Host ("DIFF " + $t + ": " + @($names).Count + " differing entries (a changed method shows once per side)")
    $names | ForEach-Object { Write-Host ("  " + $_) }
}

if ($Keep) { Write-Host ("scratch kept: " + $Scratch) }
else { Remove-Item -Recurse -Force -LiteralPath $Scratch -ErrorAction SilentlyContinue }
exit $rc
