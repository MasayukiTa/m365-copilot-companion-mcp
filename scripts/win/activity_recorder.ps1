# What actually ran, and where it talked to.
#
# WHY. An external review's advice on whether to rotate credentials rested on this sentence:
# "if arbitrary third-party code ran locally with outbound network access, review and rotate
# any high-value credentials readable by this user". The honest answer to "did it" was that
# nobody could say, because nothing was recording. Absence of evidence was being read as
# evidence of absence, and the cost of disproving a compromise is unbounded while the cost of
# writing down what happened is a few kilobytes an hour.
#
# This is an AUDIT TRAIL, not a control. It stops nothing. It exists so that the next time
# the question is asked there is something to look at, and so that a claim like "only the
# project's own build system ran" can be checked rather than assumed.
#
# It records NEW things only -- a process the first time its pid and start time are seen, a
# destination the first time that (process, address, port) triple is seen. Polling the full
# list every cycle would produce a file nobody reads.
[CmdletBinding()]
param(
    [string]$Out = ".fleet/activity.jsonl",
    [int]$IntervalSec = 20,
    [int]$MaxHours = 24
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo

$seenProc = New-Object 'System.Collections.Generic.HashSet[string]'
$seenConn = New-Object 'System.Collections.Generic.HashSet[string]'
$deadline = (Get-Date).AddHours($MaxHours)

function Emit($obj) {
    # Written as one JSON object per line, appended, so a reader can tail it and a crash
    # loses at most the current line.
    $json = $obj | ConvertTo-Json -Compress -Depth 4
    Add-Content -Path $Out -Value $json -Encoding utf8
}

Emit @{ t = (Get-Date -Format o); kind = "recorder_start"; interval_s = $IntervalSec }

# Seed WITHOUT emitting: everything already running predates this recorder, and reporting it
# as newly started would put a false timestamp on it.
foreach ($p in (Get-CimInstance Win32_Process -EA SilentlyContinue)) {
    [void]$seenProc.Add("$($p.ProcessId)|$($p.CreationDate)")
}
Emit @{ t = (Get-Date -Format o); kind = "seeded"; processes = $seenProc.Count }

while ((Get-Date) -lt $deadline) {
    try {
        foreach ($p in (Get-CimInstance Win32_Process -EA SilentlyContinue)) {
            $key = "$($p.ProcessId)|$($p.CreationDate)"
            if ($seenProc.Add($key)) {
                Emit @{
                    t      = (Get-Date -Format o)
                    kind   = "process"
                    pid    = $p.ProcessId
                    ppid   = $p.ParentProcessId
                    name   = $p.Name
                    # Truncated: a full command line can be tens of kilobytes for a build,
                    # and the first 400 characters carry the identity.
                    cmd    = if ($p.CommandLine) { $p.CommandLine.Substring(0, [Math]::Min(400, $p.CommandLine.Length)) } else { "" }
                    started = "$($p.CreationDate)"
                }
            }
        }
    } catch { }

    try {
        foreach ($c in (Get-NetTCPConnection -State Established -EA SilentlyContinue)) {
            $ra = $c.RemoteAddress
            # Loopback and link-local are not "outbound" in the sense the question asks about.
            if ($ra -eq "127.0.0.1" -or $ra -eq "::1" -or $ra -like "169.254.*" -or $ra -like "fe80:*") { continue }
            $key = "$($c.OwningProcess)|$ra|$($c.RemotePort)"
            if ($seenConn.Add($key)) {
                $pn = ""
                try { $pn = (Get-Process -Id $c.OwningProcess -EA SilentlyContinue).ProcessName } catch { }
                Emit @{
                    t     = (Get-Date -Format o)
                    kind  = "outbound"
                    pid   = $c.OwningProcess
                    proc  = $pn
                    addr  = $ra
                    port  = $c.RemotePort
                }
            }
        }
    } catch { }

    Start-Sleep -Seconds $IntervalSec
}
Emit @{ t = (Get-Date -Format o); kind = "recorder_stop"; processes = $seenProc.Count; destinations = $seenConn.Count }
