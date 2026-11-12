# test_devtunnel_token_cache_state.Tests.ps1 -- Pester 3.4.0 unit tests for goal (C):
# the supervisor must treat a 0-byte devtunnel token cache as a DISTINCT state, alert on it
# (naming the file and its mtime), NEVER call the devtunnel CLI while the cache is 0 B, and
# leave an existing live host untouched. The functions under test live in supervisor.ps1:
#   * Get-DevtunnelTokenCacheState    -- PURE: path -> 'missing'|'empty'|'present'
#   * Get-DevtunnelTokenCacheAlertLine -- PURE: builds the alert line (names file + mtime)
#   * Test-DevtunnelLoggedInCached     -- its 0-byte gate returns $false WITHOUT calling the CLI
#
# supervisor.ps1 CANNOT be dot-sourced here: it runs the live supervisor loop at the bottom of
# the file, so sourcing it would start a supervisor against the live server and tunnel. Instead
# we EXTRACT just the function (and the one script-scope flag) under test from the source text
# and evaluate those, which has no side effects. Same approach as
# test_main_launch_log.Tests.ps1 and test_a_vanished_server_is_relaunched_at_once.Tests.ps1.

function Import-SupervisorFunction([string]$Name) {
    $src = Get-Content -Raw (Join-Path $PSScriptRoot "supervisor.ps1")
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

Describe "Get-DevtunnelTokenCacheState" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Get-DevtunnelTokenCacheState")
        $script:tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("dttok_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $script:tmpRoot -Force | Out-Null
    }
    AfterAll {
        if ($script:tmpRoot -and (Test-Path $script:tmpRoot)) { Remove-Item -Recurse -Force $script:tmpRoot }
    }

    It "reports 'missing' when the file does not exist" {
        $p = Join-Path $script:tmpRoot "nope.bin"
        Get-DevtunnelTokenCacheState -Path $p | Should Be 'missing'
    }

    It "reports 'missing' for a null or blank path (never 'empty')" {
        Get-DevtunnelTokenCacheState -Path $null | Should Be 'missing'
        Get-DevtunnelTokenCacheState -Path ""   | Should Be 'missing'
        Get-DevtunnelTokenCacheState -Path "   " | Should Be 'missing'
    }

    It "reports 'empty' for a 0-byte file (the truncated state)" {
        $p = Join-Path $script:tmpRoot "zero.bin"
        Set-Content -LiteralPath $p -Value ([byte[]]@()) -Encoding Byte
        (Get-Item -LiteralPath $p).Length | Should Be 0
        Get-DevtunnelTokenCacheState -Path $p | Should Be 'empty'
    }

    It "reports 'present' for a file with bytes" {
        $p = Join-Path $script:tmpRoot "full.bin"
        Set-Content -LiteralPath $p -Value ([byte[]](1..200)) -Encoding Byte
        (Get-Item -LiteralPath $p).Length | Should BeGreaterThan 0
        Get-DevtunnelTokenCacheState -Path $p | Should Be 'present'
    }

    It "reports 'missing' for a directory (not a token file)" {
        $d = Join-Path $script:tmpRoot "adir"
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Get-DevtunnelTokenCacheState -Path $d | Should Be 'missing'
    }
}

Describe "Get-DevtunnelTokenCacheAlertLine" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Get-DevtunnelTokenCacheAlertLine")
    }

    It "names the file path in the alert" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\DevTunnels\devtunnels-tokens-microsoft" -Mtime ([datetime]"2026-11-12T00:55:47")
        $line | Should Match ([regex]::Escape("C:\x\DevTunnels\devtunnels-tokens-microsoft"))
    }

    It "names the mtime in the alert" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\tok" -Mtime ([datetime]"2026-11-12T00:55:47")
        $line | Should Match "2026-11-12 00:55:47"
    }

    It "says the CLI will not be called while empty" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\tok" -Mtime ([datetime]"2026-11-12T00:55:47")
        $line | Should Match "NOT calling the devtunnel CLI"
    }

    It "says the existing live host is left untouched" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\tok" -Mtime ([datetime]"2026-11-12T00:55:47")
        $line | Should Match "existing live host"
    }

    It "reports mtime 'unknown' when the mtime is null" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\tok" -Mtime $null
        $line | Should Match "mtime=unknown"
    }

    It "calls the 0-byte fact out explicitly" {
        $line = Get-DevtunnelTokenCacheAlertLine -Path "C:\x\tok" -Mtime $null
        $line | Should Match "0 BYTES"
    }
}

Describe "Test-DevtunnelLoggedInCached 0-byte gate" {
    # Build a sandbox where the only live things are: the function under test, the two PURE
    # helpers it calls, the path resolver (pointed at a COPY via \$env:LOCALAPPDATA), the script
    # flags it reads, and STUBS for Write-Log and Test-DevtunnelLoggedIn. The stub for
    # Test-DevtunnelLoggedIn records whether it was called, which is exactly the thing (C)
    # forbids while the cache is 0 B.
    BeforeAll {
        . (Join-Path $PSScriptRoot "token_cache_gate_fixture.ps1")
    }
    BeforeEach {
        $script:tmpLocalAppData = Join-Path ([System.IO.Path]::GetTempPath()) ("dtgate_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path (Join-Path $script:tmpLocalAppData "DevTunnels") -Force | Out-Null
        $env:LOCALAPPDATA = $script:tmpLocalAppData
        $script:CliCalls = 0
        $script:LoggedLines = New-Object System.Collections.ArrayList
        $script:DevtunnelLoginCachedUntil = [datetime]::MinValue
        $script:DevtunnelLoginAnswerWasCached = $false
        $script:DevtunnelLoginCacheUseLogged = $false
        $script:DevtunnelLoginRecheckReason = "test"
        $script:DevtunnelTokenCacheZeroAlerted = $false
        $script:StubLoggedInAnswer = $true   # if the CLI WERE called it would say "logged in"
    }
    AfterEach {
        if ($script:tmpLocalAppData -and (Test-Path $script:tmpLocalAppData)) {
            Remove-Item -Recurse -Force $script:tmpLocalAppData -ErrorAction SilentlyContinue
        }
    }

    It "returns \$false on a 0-byte cache WITHOUT calling the devtunnel CLI" {
        $p = Join-Path $script:tmpLocalAppData "DevTunnels\devtunnels-tokens-microsoft"
        Set-Content -LiteralPath $p -Value ([byte[]]@()) -Encoding Byte
        $result = Test-DevtunnelLoggedInCached
        $result | Should Be $false
        $script:CliCalls | Should Be 0
    }

    It "alerts exactly once per truncation episode, naming the file" {
        $p = Join-Path $script:tmpLocalAppData "DevTunnels\devtunnels-tokens-microsoft"
        Set-Content -LiteralPath $p -Value ([byte[]]@()) -Encoding Byte
        Test-DevtunnelLoggedInCached | Out-Null
        Test-DevtunnelLoggedInCached | Out-Null
        $alertLines = @($script:LoggedLines | Where-Object { $_ -match '0 BYTES' })
        $alertLines.Count | Should Be 1
        $alertLines[0] | Should Match "devtunnels-tokens-microsoft"
    }

    It "a present cache after a 0-byte episode DOES call the CLI and re-arms the alert" {
        $p = Join-Path $script:tmpLocalAppData "DevTunnels\devtunnels-tokens-microsoft"
        Set-Content -LiteralPath $p -Value ([byte[]]@()) -Encoding Byte
        Test-DevtunnelLoggedInCached | Out-Null          # episode 1: gated, CLI not called
        $script:CliCalls | Should Be 0
        Set-Content -LiteralPath $p -Value ("x" * 2000) -Encoding Ascii  # token restored (>0 bytes)
        $result = Test-DevtunnelLoggedInCached           # now the CLI may run
        $script:CliCalls | Should Be 1
        $result | Should Be $true
        $script:DevtunnelTokenCacheZeroAlerted | Should Be $false
    }

    It "a missing cache is handled live (CLI called) -- not confused with 0-byte" {
        # no token file created at all
        Test-DevtunnelLoggedInCached | Out-Null
        $script:CliCalls | Should Be 1
        @($script:LoggedLines | Where-Object { $_ -match '0 BYTES' }).Count | Should Be 0
    }
}
