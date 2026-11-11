# test_rehost_ghost_establish.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE establish
# decider Test-RehostEstablished in supervisor.ps1.
#
# THE BUG THIS GUARDS (2026-11-10). A "stale" (ghost) tunnel re-host leaves the relay still
# counting the dead host's connection(s) for 15+ minutes -- a count this machine has NO host
# process for, and which devtunnel offers no command to drop (no `unhost`; `delete` destroys the
# tunnel). Start-TunnelHost's old establish test was `Test-TunnelHosting` (connection count >= 1),
# which a lingering ghost satisfies ON ITS OWN. So the loop could log "established (after ~2s)"
# before the new host ever registered -- the ghost's own connection read as ours. The fix records
# the ghost's connection count BEFORE launching and treats the re-host as established only once the
# count RISES ABOVE that baseline AND a host process of ours is running. This file pins that logic.
#
# supervisor.ps1 CANNOT be dot-sourced here: it does real work at the bottom of the file (the live
# supervisor loop), so sourcing it would start a supervisor against the live server and tunnel.
# Instead we EXTRACT just the pure function under test from the source text and evaluate that, which
# has no side effects -- the same approach as test_a_vanished_server_is_relaunched_at_once.Tests.ps1.

function Import-SupervisorFunction([string]$Name) {
    $src = Get-Content -Raw (Join-Path $PSScriptRoot "supervisor.ps1")
    # Match "function <Name> {" and take text up to the matching close brace by brace-counting.
    $marker = "function $Name"
    $idx = $src.IndexOf($marker)
    if ($idx -lt 0) { throw "function $Name not found in supervisor.ps1" }
    $brace = $src.IndexOf("{", $idx)
    if ($brace -lt 0) { throw "no opening brace for $Name" }
    $depth = 0
    $i = $brace
    while ($i -lt $src.Length) {
        $ch = $src[$i]
        if ($ch -eq "{") { $depth++ }
        elseif ($ch -eq "}") { $depth--; if ($depth -eq 0) { break } }
        $i++
    }
    return $src.Substring($idx, ($i - $idx + 1))
}

Describe "Test-RehostEstablished" {
    # Pester 5 (CI) evaluates file-scope code during discovery, so re-import inside the run phase
    # too; Pester 3.4.0 (this machine) honours BeforeAll as well, so one file works on both.
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Test-RehostEstablished")
    }

    # ---- the ghost case the fix exists for -------------------------------------------------
    It "ghost lingers at baseline, no host of ours -> NOT established (the old false positive)" {
        Test-RehostEstablished -NowConn 1 -BaselineConn 1 -OurHostRunning $false | Should Be $false
    }

    It "ghost lingers at baseline, even WITH our host running, count has not risen -> NOT established" {
        Test-RehostEstablished -NowConn 1 -BaselineConn 1 -OurHostRunning $true | Should Be $false
    }

    It "count rises above the ghost baseline AND our host runs -> established" {
        Test-RehostEstablished -NowConn 2 -BaselineConn 1 -OurHostRunning $true | Should Be $true
    }

    It "count rose above baseline but NO host of ours running -> NOT established (could be another PC)" {
        Test-RehostEstablished -NowConn 2 -BaselineConn 1 -OurHostRunning $false | Should Be $false
    }

    # ---- the plain "none" re-host (no ghost): baseline 0 reduces to the old behaviour ---------
    It "no ghost (baseline 0): first connection with our host running -> established" {
        Test-RehostEstablished -NowConn 1 -BaselineConn 0 -OurHostRunning $true | Should Be $true
    }

    It "no ghost (baseline 0): still 0 connections -> NOT established" {
        Test-RehostEstablished -NowConn 0 -BaselineConn 0 -OurHostRunning $true | Should Be $false
    }

    It "no ghost (baseline 0): a connection but no host of ours -> NOT established" {
        Test-RehostEstablished -NowConn 1 -BaselineConn 0 -OurHostRunning $false | Should Be $false
    }

    # ---- devtunnel show gave no readable count -----------------------------------------------
    It "null current count -> NOT established, whatever the baseline or host state" {
        Test-RehostEstablished -NowConn $null -BaselineConn 0 -OurHostRunning $true | Should Be $false
        Test-RehostEstablished -NowConn $null -BaselineConn 1 -OurHostRunning $true | Should Be $false
    }

    It "null baseline is treated as 0 (no ghost) -> first connection with our host established" {
        Test-RehostEstablished -NowConn 1 -BaselineConn $null -OurHostRunning $true | Should Be $true
    }

    # ---- a ghost of 2+ connections ----------------------------------------------------------
    It "two ghost connections lingering (baseline 2), count still 2 -> NOT established" {
        Test-RehostEstablished -NowConn 2 -BaselineConn 2 -OurHostRunning $true | Should Be $false
    }

    It "two ghost connections, count climbs to 3 with our host -> established" {
        Test-RehostEstablished -NowConn 3 -BaselineConn 2 -OurHostRunning $true | Should Be $true
    }
}
