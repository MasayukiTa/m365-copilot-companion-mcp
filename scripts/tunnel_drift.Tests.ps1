# tunnel_drift.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE helpers in
# tunnel_name_util.ps1 (shared by start_all.ps1, supervisor.ps1, and doctor.ps1
# to detect a running supervisor that is hosting a stale/borrowed tunnel vs.
# .env's current MCP_TUNNEL_NAME). tunnel_name_util.ps1 has no top-level side
# effects (it only defines functions), so it is dot-sourced directly here --
# no `$MyInvocation.InvocationName -ne '.'` guard is needed (unlike
# heal_tunnel.ps1, which does real work at the bottom of its file).

$scriptPath = Join-Path $PSScriptRoot "tunnel_name_util.ps1"
. $scriptPath

Describe "Get-SupervisorArgTunnel" {

    It "extracts the tunnel name from a realistic supervisor command line" {
        $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\repo\scripts\supervisor.ps1 -TunnelName foo.jpe1'
        Get-SupervisorArgTunnel $cmd | Should Be "foo.jpe1"
    }

    It "returns empty string when there is no -TunnelName on the command line" {
        $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\repo\scripts\supervisor.ps1'
        Get-SupervisorArgTunnel $cmd | Should Be ""
    }

    It "returns empty string for a null or empty command line" {
        Get-SupervisorArgTunnel "" | Should Be ""
        Get-SupervisorArgTunnel $null | Should Be ""
    }
}

Describe "Get-BareTunnelName" {

    It "strips the .cluster suffix" {
        Get-BareTunnelName "foo.jpe1" | Should Be "foo"
    }

    It "leaves an already-bare name unchanged, lowercased" {
        Get-BareTunnelName "FOO" | Should Be "foo"
    }

    It "returns empty string for a null or empty name" {
        Get-BareTunnelName "" | Should Be ""
        Get-BareTunnelName $null | Should Be ""
    }
}

Describe "bare-name compare" {

    It "foo.jpe1 vs foo.use2 compare EQUAL (same bare name)" {
        (Get-BareTunnelName "foo.jpe1") | Should Be (Get-BareTunnelName "foo.use2")
    }

    It "foo vs bar compare NOT equal" {
        (Get-BareTunnelName "foo") | Should Not Be (Get-BareTunnelName "bar")
    }
}

Describe "Test-SupervisorTunnelDrift" {

    It "running=borrowed, env=mine (different bare names) -> drifted, restart" {
        $cmd = 'powershell.exe -NoProfile -File C:\repo\scripts\supervisor.ps1 -TunnelName borrowed.usw2'
        Test-SupervisorTunnelDrift -RunningCommandLine $cmd -EnvTunnelName "mine.usw2" | Should Be $true
    }

    It "running and env share the same bare name (different cluster) -> leave as-is" {
        $cmd = 'powershell.exe -NoProfile -File C:\repo\scripts\supervisor.ps1 -TunnelName mine.usw2'
        Test-SupervisorTunnelDrift -RunningCommandLine $cmd -EnvTunnelName "mine.use2" | Should Be $false
    }

    It "running and env are identical -> leave as-is" {
        $cmd = 'powershell.exe -NoProfile -File C:\repo\scripts\supervisor.ps1 -TunnelName mine.usw2'
        Test-SupervisorTunnelDrift -RunningCommandLine $cmd -EnvTunnelName "mine.usw2" | Should Be $false
    }

    It "running has no -TunnelName (unknown) -> conservative leave as-is" {
        $cmd = 'powershell.exe -NoProfile -File C:\repo\scripts\supervisor.ps1'
        Test-SupervisorTunnelDrift -RunningCommandLine $cmd -EnvTunnelName "mine.usw2" | Should Be $false
    }

    It ".env name is empty (unknown) -> conservative leave as-is" {
        $cmd = 'powershell.exe -NoProfile -File C:\repo\scripts\supervisor.ps1 -TunnelName borrowed.usw2'
        Test-SupervisorTunnelDrift -RunningCommandLine $cmd -EnvTunnelName "" | Should Be $false
    }
}
