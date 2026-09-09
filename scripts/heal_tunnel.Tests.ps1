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
# PESTER 5 RUNS FILE-SCOPE CODE DURING DISCOVERY, NOT DURING THE RUN. The dot-source above
# is enough for Pester 3.4.0 (what this machine has) and useless on the CI runner (which picks
# Pester 5): there, these functions are defined while tests are being FOUND and are gone by the
# time any It executes -- "Discovery found 13 tests" and then 13 identical
# CommandNotFoundException failures. Repeating the dot-source in a BeforeAll inside each
# Describe puts them in the run phase as well; 3.4.0 honours BeforeAll too, so one file works
# on both and neither version needs pinning.
    # $scriptPath is a FILE-SCOPE variable, so under Pester 5 it belongs to the discovery
    # phase and is null by the time this runs -- `. $null` fails with "the expression after
    # '.' ... must result in a command name". Rebuild the path here, where $PSScriptRoot is
    # available in both phases.
    BeforeAll { . (Join-Path $PSScriptRoot "heal_tunnel.ps1") }


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

Describe "Get-OwnedTunnelIdsFromListOutput" {
# PESTER 5 RUNS FILE-SCOPE CODE DURING DISCOVERY, NOT DURING THE RUN. The dot-source above
# is enough for Pester 3.4.0 (what this machine has) and useless on the CI runner (which picks
# Pester 5): there, these functions are defined while tests are being FOUND and are gone by the
# time any It executes -- "Discovery found 13 tests" and then 13 identical
# CommandNotFoundException failures. Repeating the dot-source in a BeforeAll inside each
# Describe puts them in the run phase as well; 3.4.0 honours BeforeAll too, so one file works
# on both and neither version needs pinning.
    # $scriptPath is a FILE-SCOPE variable, so under Pester 5 it belongs to the discovery
    # phase and is null by the time this runs -- `. $null` fails with "the expression after
    # '.' ... must result in a command name". Rebuild the path here, where $PSScriptRoot is
    # available in both phases.
    BeforeAll { . (Join-Path $PSScriptRoot "heal_tunnel.ps1") }


    # MEASURED 2026-09-09: heal_tunnel.ps1's Invoke-DevTunnelBounded runs the CLI as
    # `& $exe @a 2>&1 | Out-String`, merging stderr into the text that gets parsed as
    # data. When the CLI errors (this repo's evidence: invalid_token auth failures at
    # 07:35-07:36 in .setup/logs/server.err.history.log), PowerShell renders the merged
    # native-command stderr as a line starting with the executable name -- confirmed
    # against the real regex: "devtunnel.exe : Error: ..." matches
    # '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s' the same way "resonac-mcp.jpe1 " does, and
    # the old (pre-fix) extraction reduced it via Get-BareTunnelId to the bare id
    # "devtunnel" -- an id nobody owns, believed anyway because parsing SUCCEEDED (Count
    # -eq 1), so the "could not parse" guard never fired. This is the mechanism behind
    # the 2026-09-09 07:36 corruption of MCP_TUNNEL_NAME from 'resonac-mcp' to
    # 'devtunnel'.

    It "parses a real tunnel row" {
        $listOut = @(
            "List of tunnels:"
            ""
            "ID                Description  Host Connections  Client Connections  Ports"
            "----------------  -----------  ----------------  ------------------  -----"
            "resonac-mcp.jpe1                          1                    0        8000"
        ) -join "`r`n"
        $ids = Get-OwnedTunnelIdsFromListOutput $listOut
        $ids | Should Be @("resonac-mcp.jpe1")
    }

    It "does NOT parse a merged PowerShell native-command error line as a tunnel id (THE BUG)" {
        # This is PowerShell's actual rendering of a merged native-command stderr line --
        # not a paraphrase. Reproduced by running a failing native exe through
        # `2>&1 | Out-String` (the exact pattern Invoke-DevTunnelBounded uses).
        $listOut = @(
            "devtunnel.exe : Error: unable to list tunnels: 401 invalid_token"
            "    + CategoryInfo          : NotSpecified: (Error: unable ...:String) [], RemoteException"
            "    + FullyQualifiedErrorId : NativeCommandError"
        ) -join "`r`n"
        $ids = Get-OwnedTunnelIdsFromListOutput $listOut
        $ids.Count | Should Be 0
        ($ids -contains "devtunnel") | Should Be $false
    }

    It "parses the real row and rejects the error line when both appear in the same merged output" {
        # This is the exact shape a genuinely-owned account with a mid-listing auth
        # hiccup would produce: a good row plus a merged stderr line in the same
        # Invoke-DevTunnelBounded output.
        $listOut = @(
            "resonac-mcp.jpe1                          1                    0        8000"
            "devtunnel.exe : Error: unable to refresh token: 401 invalid_token"
        ) -join "`r`n"
        $ids = Get-OwnedTunnelIdsFromListOutput $listOut
        $ids | Should Be @("resonac-mcp.jpe1")
        ($ids -contains "devtunnel") | Should Be $false
    }

    It "empty/null input -> no ids, no throw" {
        (Get-OwnedTunnelIdsFromListOutput "").Count | Should Be 0
        (Get-OwnedTunnelIdsFromListOutput $null).Count | Should Be 0
    }
}

Describe "Test-LooksLikeExecutableSuffix" {
# PESTER 5 RUNS FILE-SCOPE CODE DURING DISCOVERY, NOT DURING THE RUN. The dot-source above
# is enough for Pester 3.4.0 (what this machine has) and useless on the CI runner (which picks
# Pester 5): there, these functions are defined while tests are being FOUND and are gone by the
# time any It executes -- "Discovery found 13 tests" and then 13 identical
# CommandNotFoundException failures. Repeating the dot-source in a BeforeAll inside each
# Describe puts them in the run phase as well; 3.4.0 honours BeforeAll too, so one file works
# on both and neither version needs pinning.
    # $scriptPath is a FILE-SCOPE variable, so under Pester 5 it belongs to the discovery
    # phase and is null by the time this runs -- `. $null` fails with "the expression after
    # '.' ... must result in a command name". Rebuild the path here, where $PSScriptRoot is
    # available in both phases.
    BeforeAll { . (Join-Path $PSScriptRoot "heal_tunnel.ps1") }


    It "flags .exe and sibling executable/script suffixes" {
        Test-LooksLikeExecutableSuffix "exe" | Should Be $true
        Test-LooksLikeExecutableSuffix "EXE" | Should Be $true
        Test-LooksLikeExecutableSuffix "ps1" | Should Be $true
        Test-LooksLikeExecutableSuffix "bat" | Should Be $true
    }

    It "does not flag real devtunnel cluster codes" {
        Test-LooksLikeExecutableSuffix "jpe1" | Should Be $false
        Test-LooksLikeExecutableSuffix "usw2" | Should Be $false
        Test-LooksLikeExecutableSuffix "use2" | Should Be $false
    }
}
