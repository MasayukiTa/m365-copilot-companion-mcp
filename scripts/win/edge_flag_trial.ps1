# What does an idle managed Edge actually cost, and which of it is optional?
#
# The browser has to stay alive between captures: it holds the sign-in, and killing it means
# authenticating again. So its idle footprint is a floor, and the question is how low the floor
# is. Task Manager on the running instance:
#
#     GPU process             51.1 MB      <- the largest single item
#     browser                 42.0 MB
#     tab: about:blank         9.6 MB      <- the "tab", and the smallest part of it
#     Network Service          9.0 MB
#     spare renderer           8.9 MB
#     Storage / Indexer        4.9 MB
#     Crashpad                 0.8 MB
#                            126.3 MB
#
# ONE PAGE IS STRUCTURAL: closing the last one closes the browser.
#
# MEASURED ON A THROWAWAY PROFILE, never on the live one. A signed-in profile is not something
# to experiment on, and the question -- what an empty browser costs -- does not need one.
#
#   powershell -NoProfile -File scripts/win/edge_flag_trial.ps1

[CmdletBinding()]
param([int]$SettleSeconds = 12)

$ErrorActionPreference = "Stop"

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
if (-not (Test-Path $edge)) { throw "Edge not found" }

# The flags the companion launches with today, minus the profile and port which vary per arm.
$base = @(
    "--headless=new",
    "--no-first-run", "--no-default-browser-check",
    "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-extensions", "--disable-sync", "--disable-component-update",
    "--process-per-site"
)

# THE FEATURE LIST MUST BE ONE FLAG. Chromium takes the LAST --disable-features and ignores
# every earlier one, so adding a second flag silently drops Translate, MediaRouter and
# OptimizationHints -- a change that looks additive and is not.
$featuresNow = "--disable-features=Translate,MediaRouter,OptimizationHints"
$featuresLean = "--disable-features=Translate,MediaRouter,OptimizationHints,SpareRendererForSitePerProcess"

$arms = @(
    @{ Name = "as launched today"; Extra = @($featuresNow) },
    @{ Name = "no spare renderer"; Extra = @($featuresLean) },
    @{ Name = "no gpu"; Extra = @($featuresNow, "--disable-gpu") },
    @{ Name = "no gpu, no spare, no crashpad";
       Extra = @($featuresLean, "--disable-gpu", "--disable-crash-reporter",
                 "--disable-breakpad") }
)

function Measure-Arm($name, $extra, $port) {
    $dir = Join-Path $env:TEMP ("edge_flag_trial_" + $port)
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue }
    $args = $base + $extra + @(
        "--user-data-dir=$dir",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=$port",
        "about:blank")
    $proc = Start-Process -FilePath $edge -ArgumentList $args -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds $SettleSeconds
    # PRIVATE working set, not WorkingSetSize. The latter includes shared pages, and a
    # browser is fifteen processes sharing one binary -- summing it reported 781 MB for an
    # empty headless browser that Task Manager showed as 126.
    $rows = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -like "*edge_flag_trial_$port*" }
    $perf = Get-CimInstance Win32_PerfRawData_PerfProc_Process -Filter "Name like 'msedge%'"
    $priv = @{}
    foreach ($q in $perf) { $priv[[int]$q.IDProcess] = [double]$q.WorkingSetPrivate }
    function PrivOf($id) { if ($priv.ContainsKey([int]$id)) { return $priv[[int]$id] } return 0 }
    $total = 0
    foreach ($r in $rows) { $total += (PrivOf $r.ProcessId) }
    $total = $total / 1MB
    $byType = @{}
    foreach ($r in $rows) {
        $t = "browser"
        if ($r.CommandLine -match "--type=([a-zA-Z-]+)") { $t = $Matches[1] }
        if ($r.CommandLine -match "--utility-sub-type=\S*?([A-Za-z]+)Service") { $t = "utility:" + $Matches[1] }
        $byType[$t] = [math]::Round(($byType[$t] + (PrivOf $r.ProcessId) / 1MB), 1)
    }
    $detail = ($byType.GetEnumerator() | Sort-Object Value -Descending |
               ForEach-Object { "{0}={1}" -f $_.Key, $_.Value }) -join " "
    Write-Output ("{0,-32} {1,6:N1} MB  procs={2}  {3}" -f $name, $total, $rows.Count, $detail)
    foreach ($r in $rows) {
        try { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Seconds 2
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue }
}

$port = 9330
foreach ($arm in $arms) {
    Measure-Arm $arm.Name $arm.Extra $port
    $port++
}
