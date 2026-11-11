# supervisor_watchdog.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE helpers in
# supervisor_watchdog.ps1. Hermetic: no disk, no process. The file is dot-sourced, and its
# MAIN body is behind a `$MyInvocation.InvocationName -ne '.'` guard, so dot-sourcing defines
# the functions without reading a heartbeat or starting start_all.
#
# ConvertTo-WatchdogDecision is kept byte-for-byte equivalent to the Python gate
# (scripts/supervisor_watchdog_gate.decide_watchdog_action, pinned on Linux by
# test_supervisor_watchdog_gate.py). The two test files share the same boundary cases so the
# PowerShell and Python copies cannot drift.
#
# ASCII / ENGLISH ONLY. No BOM.

$scriptPath = Join-Path $PSScriptRoot "supervisor_watchdog.ps1"
. $scriptPath

Describe "ConvertTo-WatchdogDecision" {

    It "relaunches when the supervisor is dead and the heartbeat is stale" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 181 -SupervisorAlive $false -StaleThresholdSec 180 | Should Be "relaunch"
    }

    It "relaunches when the supervisor is dead and there is no heartbeat" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec $null -SupervisorAlive $false -StaleThresholdSec 180 | Should Be "relaunch"
    }

    It "waits when the supervisor is alive even with a stale heartbeat" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 10000 -SupervisorAlive $true -StaleThresholdSec 180 | Should Be "wait"
    }

    It "waits when the supervisor is alive and there is no heartbeat" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec $null -SupervisorAlive $true -StaleThresholdSec 180 | Should Be "wait"
    }

    It "waits when the supervisor is dead but the heartbeat is still fresh" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 179 -SupervisorAlive $false -StaleThresholdSec 180 | Should Be "wait"
    }

    It "treats age exactly at the threshold as stale (relaunch when dead)" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 180 -SupervisorAlive $false -StaleThresholdSec 180 | Should Be "relaunch"
    }

    It "waits when liveness is unknown (null) even if stale -- fail safe" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 10000 -SupervisorAlive $null -StaleThresholdSec 180 | Should Be "wait"
    }

    It "waits when the window is non-positive (a broken threshold must not look stale)" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec 10000 -SupervisorAlive $false -StaleThresholdSec 0 | Should Be "wait"
    }

    It "accepts a numeric string age handed in from a shell" {
        ConvertTo-WatchdogDecision -HeartbeatAgeSec "999999" -SupervisorAlive $false -StaleThresholdSec 180 | Should Be "relaunch"
    }
}

Describe "Get-HeartbeatPidAndAge" {

    $now = [datetime]::ParseExact("2026-11-11 16:00:00", "yyyy-MM-dd HH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture)

    It "parses pid and computes age from local_time" {
        $body = '{"pid":25644,"local_time":"2026-11-11 15:58:00","iteration":7}'
        $r = Get-HeartbeatPidAndAge -Body $body -Now $now
        $r.pid | Should Be 25644
        $r.age_sec | Should Be 120
    }

    It "returns null age for a body that is not JSON" {
        $r = Get-HeartbeatPidAndAge -Body '<html>nope</html>' -Now $now
        $r.age_sec | Should Be $null
    }

    It "returns null age and null pid for an empty body" {
        $r = Get-HeartbeatPidAndAge -Body '' -Now $now
        $r.age_sec | Should Be $null
        $r.pid | Should Be $null
    }

    It "returns null age when local_time is missing, but still reads the pid" {
        $r = Get-HeartbeatPidAndAge -Body '{"pid":10}' -Now $now
        $r.pid | Should Be 10
        $r.age_sec | Should Be $null
    }

    It "returns null age when local_time is unparseable" {
        $r = Get-HeartbeatPidAndAge -Body '{"pid":10,"local_time":"not a date"}' -Now $now
        $r.age_sec | Should Be $null
    }
}

Describe "Format-WatchdogResultLine" {

    It "reports a relaunch decision with the supervisor pid and age" {
        $line = Format-WatchdogResultLine -Decision "relaunch" -AgeSec 240.0 -SupervisorPid 25644 -SupervisorAlive $false
        $line | Should Match 'decision=relaunch'
        $line | Should Match 'supervisor_pid=25644'
        $line | Should Match 'alive=no'
        $line | Should Match 'heartbeat_age=240\.0s'
    }

    It "renders a null age as n/a and unknown liveness" {
        $line = Format-WatchdogResultLine -Decision "wait" -AgeSec $null -SupervisorPid $null -SupervisorAlive $null
        $line | Should Match 'heartbeat_age=n/a'
        $line | Should Match 'supervisor_pid=none'
        $line | Should Match 'alive=unknown'
    }
}
