# test_a_vanished_server_is_relaunched_at_once.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE
# vanished-server decision in supervisor.ps1:
#   * Get-VanishedServerVerdict  -- the truth table that decides relaunch-now vs wait vs strike
#   * Test-ServerTransitionActive -- reads the advisory planned-restart marker
#
# supervisor.ps1 CANNOT be dot-sourced here: unlike tunnel_name_util.ps1 it does real work at the
# bottom of the file (try { while/for (;;) { ... } } -- the live supervisor loop), so sourcing it
# would start a supervisor against the live server and tunnel. Instead we EXTRACT just the two
# pure function definitions from the source text and evaluate those, which has no side effects.
# This keeps the test honest (it runs the real function bodies shipped in supervisor.ps1) without
# ever launching anything.

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

Describe "Get-VanishedServerVerdict" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Get-VanishedServerVerdict")
    }

    # TRUTH TABLE (Listening, OwnMainPyAlive, TransitionActive) -> verdict
    #
    #  Listening | MainPyAlive | Transition | verdict
    #  ----------+-------------+------------+-------------
    #    false   |   false     |   false    | relaunch-now   <- the vanished case: act at once
    #    false   |   false     |   true     | wait           <- planned restart owns the gap
    #    false   |   true      |   false    | wait           <- mid-boot, caller grace protects it
    #    false   |   true      |   true     | wait
    #    true    |   false     |   false    | wait           <- something is listening
    #    true    |   false     |   true     | wait
    #    true    |   true      |   false    | wait
    #    true    |   true      |   true     | wait

    It "relaunch-now ONLY when nothing listens, no main.py of ours, and no planned transition" {
        Get-VanishedServerVerdict -Listening $false -OwnMainPyAlive $false -TransitionActive $false | Should Be "relaunch-now"
    }

    It "waits when a planned transition is in progress even though the server is absent" {
        Get-VanishedServerVerdict -Listening $false -OwnMainPyAlive $false -TransitionActive $true | Should Be "wait"
    }

    It "waits when our main.py is alive (mid-boot), transition off" {
        Get-VanishedServerVerdict -Listening $false -OwnMainPyAlive $true -TransitionActive $false | Should Be "wait"
    }

    It "waits when our main.py is alive (mid-boot), transition on" {
        Get-VanishedServerVerdict -Listening $false -OwnMainPyAlive $true -TransitionActive $true | Should Be "wait"
    }

    It "waits when something is listening, no main.py, transition off" {
        Get-VanishedServerVerdict -Listening $true -OwnMainPyAlive $false -TransitionActive $false | Should Be "wait"
    }

    It "waits when something is listening, no main.py, transition on" {
        Get-VanishedServerVerdict -Listening $true -OwnMainPyAlive $false -TransitionActive $true | Should Be "wait"
    }

    It "waits when something is listening and our main.py is alive, transition off" {
        Get-VanishedServerVerdict -Listening $true -OwnMainPyAlive $true -TransitionActive $false | Should Be "wait"
    }

    It "waits when something is listening and our main.py is alive, transition on" {
        Get-VanishedServerVerdict -Listening $true -OwnMainPyAlive $true -TransitionActive $true | Should Be "wait"
    }

    It "never returns relaunch-now unless the server has truly vanished" {
        # Exhaustive: the only relaunch-now row is (false,false,false).
        foreach ($L in @($true,$false)) {
            foreach ($M in @($true,$false)) {
                foreach ($T in @($true,$false)) {
                    $v = Get-VanishedServerVerdict -Listening $L -OwnMainPyAlive $M -TransitionActive $T
                    if (-not $L -and -not $M -and -not $T) {
                        $v | Should Be "relaunch-now"
                    } else {
                        $v | Should Be "wait"
                    }
                }
            }
        }
    }
}

Describe "Test-ServerTransitionActive" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Test-ServerTransitionActive")
    }

    It "is false when no marker file exists" {
        $script:ServerTransitionPath = Join-Path $TestDrive "no_such_transition.json"
        Test-ServerTransitionActive | Should Be $false
    }

    It "is true for an unexpired planned_restart marker" {
        $script:ServerTransitionPath = Join-Path $TestDrive "active.json"
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $body = @{ state = "planned_restart"; expires = ($now + 120) } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($script:ServerTransitionPath, $body)
        Test-ServerTransitionActive | Should Be $true
    }

    It "is false for an EXPIRED planned_restart marker (a stale file never masks a real outage)" {
        $script:ServerTransitionPath = Join-Path $TestDrive "expired.json"
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $body = @{ state = "planned_restart"; expires = ($now - 5) } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($script:ServerTransitionPath, $body)
        Test-ServerTransitionActive | Should Be $false
    }

    It "is false when the marker state is not planned_restart" {
        $script:ServerTransitionPath = Join-Path $TestDrive "other.json"
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $body = @{ state = "something_else"; expires = ($now + 120) } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($script:ServerTransitionPath, $body)
        Test-ServerTransitionActive | Should Be $false
    }

    It "is false (fails open) for a malformed marker file" {
        $script:ServerTransitionPath = Join-Path $TestDrive "garbage.json"
        [IO.File]::WriteAllText($script:ServerTransitionPath, "not json at all {{{")
        Test-ServerTransitionActive | Should Be $false
    }
}
