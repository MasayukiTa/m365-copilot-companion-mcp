# update_recovery.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE
# Get-UpdateStrategy decision helper (defined in tunnel_name_util.ps1) that
# Check-ForUpdates (start_all.ps1) uses to pick between a normal
# fast-forward update and the silent rewritten-upstream recovery path. See
# tunnel_name_util.ps1's own comment block above Get-UpdateStrategy for the
# incident this backs and the full decision table.
#
# tunnel_name_util.ps1 has no top-level side effects (it only defines
# functions), so it is dot-sourced directly here -- no
# `$MyInvocation.InvocationName -ne '.'` guard is needed (unlike
# heal_tunnel.ps1, which does real work at the bottom of its file).

$scriptPath = Join-Path $PSScriptRoot "tunnel_name_util.ps1"
. $scriptPath

Describe "Get-UpdateStrategy" {

    It "up to date: Behind 0 -> up-to-date" {
        Get-UpdateStrategy -Behind 0 -Ahead 0 -CanFastForward $false | Should Be "up-to-date"
    }

    It "already caught up (negative Behind, e.g. a parse quirk) -> up-to-date" {
        Get-UpdateStrategy -Behind -1 -Ahead 0 -CanFastForward $false | Should Be "up-to-date"
    }

    It "plain fast-forward: Behind 5, Ahead 0, CanFastForward true -> fast-forward" {
        Get-UpdateStrategy -Behind 5 -Ahead 0 -CanFastForward $true | Should Be "fast-forward"
    }

    It "the real incident shape: Behind 10, Ahead 4, CanFastForward false -> rewritten-upstream" {
        Get-UpdateStrategy -Behind 10 -Ahead 4 -CanFastForward $false | Should Be "rewritten-upstream"
    }

    It "diverged with Ahead 0 and CanFastForward false -> diverged-unknown" {
        Get-UpdateStrategy -Behind 3 -Ahead 0 -CanFastForward $false | Should Be "diverged-unknown"
    }

    It "fast-forward wins even if Ahead is also (nonsensically) positive" {
        Get-UpdateStrategy -Behind 2 -Ahead 1 -CanFastForward $true | Should Be "fast-forward"
    }

    It "nonsense/negative Ahead with Behind>0 and not fast-forwardable -> still returns a valid value" {
        # Pester 3.4's "Should Contain" checks file CONTENTS, not array membership --
        # use PowerShell's own -contains operator against the known-valid result set.
        $result = Get-UpdateStrategy -Behind 7 -Ahead -2 -CanFastForward $false
        $validValues = @("up-to-date", "fast-forward", "rewritten-upstream", "diverged-unknown")
        ($validValues -contains $result) | Should Be $true
    }
}
