# heal_tunnel.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE decision
# function Get-TunnelHealAction in heal_tunnel.ps1.
#
# heal_tunnel.ps1 runs Invoke-TunnelHeal (real devtunnel/.env I/O) as soon as
# it is invoked directly, so it cannot be `& `-ed here. Dot-sourcing it
# instead defines all its functions in this scope WITHOUT running the heal,
# because the script itself gates the bottom-of-file invocation on
# `$MyInvocation.InvocationName -ne '.'` (see heal_tunnel.ps1's own comment
# next to that guard).

$scriptPath = Join-Path $PSScriptRoot "heal_tunnel.ps1"
. $scriptPath

Describe "Get-TunnelHealAction" {

    It "owned name + matching URL -> noop" {
        $owned = @([PSCustomObject]@{ Id = "mytunnel.usw2"; Url = "https://mytunnel-abcd.usw2.devtunnels.ms/" })
        $result = Get-TunnelHealAction -Name "mytunnel.usw2" -Url "https://mytunnel-abcd.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "noop"
        $result.TargetId | Should Be "mytunnel.usw2"
    }

    It "owned name + different recorded URL (real URL known) -> url_fix with the real URL" {
        $owned = @([PSCustomObject]@{ Id = "mytunnel.usw2"; Url = "https://mytunnel-abcd.usw2.devtunnels.ms/" })
        $result = Get-TunnelHealAction -Name "mytunnel.usw2" -Url "https://oldstale-zzzz.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "url_fix"
        $result.TargetId | Should Be "mytunnel.usw2"
        $result.TargetUrl | Should Be "https://mytunnel-abcd.usw2.devtunnels.ms/"
    }

    It "owned name + empty recorded URL (real URL known) -> url_fix" {
        $owned = @([PSCustomObject]@{ Id = "mytunnel.usw2"; Url = "https://mytunnel-abcd.usw2.devtunnels.ms/" })
        $result = Get-TunnelHealAction -Name "mytunnel.usw2" -Url "" -Owned $owned
        $result.Action | Should Be "url_fix"
        $result.TargetUrl | Should Be "https://mytunnel-abcd.usw2.devtunnels.ms/"
    }

    It "owned name + real URL unknown/empty -> noop (can't safely fix)" {
        $owned = @([PSCustomObject]@{ Id = "mytunnel.usw2"; Url = "" })
        $result = Get-TunnelHealAction -Name "mytunnel.usw2" -Url "https://oldstale-zzzz.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "noop"
    }

    It "not owned, URL matches an owned tunnel -> repoint" {
        $owned = @([PSCustomObject]@{ Id = "othertunnel.usw2"; Url = "https://shared-abcd.usw2.devtunnels.ms/" })
        $result = Get-TunnelHealAction -Name "notmine.usw2" -Url "https://shared-abcd.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "repoint"
        $result.TargetId | Should Be "othertunnel.usw2"
        $result.TargetUrl | Should Be "https://shared-abcd.usw2.devtunnels.ms"
    }

    It "not owned, no URL match, owns at least one -> rename_url" {
        $owned = @([PSCustomObject]@{ Id = "othertunnel.usw2"; Url = "https://shared-abcd.usw2.devtunnels.ms/" })
        $result = Get-TunnelHealAction -Name "notmine.usw2" -Url "https://completely-different.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "rename_url"
        $result.TargetId | Should Be "othertunnel.usw2"
        $result.TargetUrl | Should Be "https://shared-abcd.usw2.devtunnels.ms/"
    }

    It "owns nothing -> setup_needed" {
        $owned = @()
        $result = Get-TunnelHealAction -Name "notmine.usw2" -Url "https://whatever.usw2.devtunnels.ms" -Owned $owned
        $result.Action | Should Be "setup_needed"
    }
}
