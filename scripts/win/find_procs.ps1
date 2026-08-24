# find_procs.ps1 -- find this project's processes without matching the search itself.
#
# THE SAME MISTAKE THREE TIMES IN ONE DAY, twice destructively.
#
#   1. A check for the minimise keeper reported "1 running" when none was. The filter looked
#      for a script name in every powershell.exe command line, and the query's OWN command line
#      contained that name. A count of one meant zero, and the eval browser sat unwatched in
#      front of the operator because of it.
#   2. A command to stop a measurement runner matched the shell that was about to start it.
#      Both attempts died at once; two diagnostic blocks recorded nothing and half an hour was
#      spent before the pattern was recognised.
#
# A substring test over command lines is a self-referential predicate: writing the pattern into
# the command makes the command a match. Two things fix it, and both are needed --
#
#   * FILTER BY PROCESS NAME FIRST. A python runner is python.exe; a keeper is a powershell.exe
#     started with -File. Nothing that merely mentions them qualifies.
#   * EXCLUDE THIS PROCESS AND ITS ANCESTORS. -like on a command line still catches the caller
#     and whatever shell wrapped it, and killing your own parent is how the second failure
#     managed to look like an unexplained exit 255.
#
# ASCII / ENGLISH ONLY.
param(
    [Parameter(Mandatory = $true)][string]$Pattern,
    [string]$ProcessName = "python.exe",
    [switch]$Stop
)

$ErrorActionPreference = "SilentlyContinue"

function Get-AncestorIds {
    # This process and every parent above it. Whatever launched us mentions the pattern too --
    # the tool call, the shell, the wrapper -- and none of them is the thing being looked for.
    $ids = @()
    $current = $PID
    for ($i = 0; $i -lt 12 -and $current; $i++) {
        $ids += $current
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$current"
        if (-not $p) { break }
        $current = $p.ParentProcessId
    }
    return $ids
}

$mine = Get-AncestorIds

$hits = @(Get-CimInstance Win32_Process -Filter "Name='$ProcessName'" |
          Where-Object { $mine -notcontains $_.ProcessId -and $_.CommandLine -like "*$Pattern*" })

if ($Stop) {
    foreach ($h in $hits) {
        try { Stop-Process -Id $h.ProcessId -Force -ErrorAction Stop; Write-Host ("stopped " + $h.ProcessId) }
        catch { Write-Host ("could not stop " + $h.ProcessId) }
    }
    Write-Host ("stopped count: " + $hits.Count)
} else {
    Write-Host ("count: " + $hits.Count)
    foreach ($h in $hits) {
        $c = $h.CommandLine
        if ($c.Length -gt 100) { $c = $c.Substring(0, 100) }
        Write-Host ("  " + $h.ProcessId + " " + $c)
    }
}
