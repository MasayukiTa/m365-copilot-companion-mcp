# token_cache_gate_fixture.ps1 -- builds the sandbox the 0-byte-gate tests run in.
#
# It extracts the four functions the gate needs from supervisor.ps1 (Get-DevtunnelTokenCachePath,
# Get-DevtunnelTokenCacheState, Get-DevtunnelTokenCacheAlertLine, Test-DevtunnelLoggedInCached)
# by their definition text -- the same side-effect-free extraction the Tests file uses -- and
# evaluates them here. It then DEFINES STUBS for the two things the gate calls that we must not
# run for real:
#   * Write-Log               -- records each line into $script:LoggedLines instead of a file.
#   * Test-DevtunnelLoggedIn  -- the devtunnel CLI wrapper. The stub increments $script:CliCalls
#                                and returns $script:StubLoggedInAnswer. The whole point of (C)
#                                is that this stub stays at 0 calls while the cache is 0 B.
# Get-DevtunnelTokenCachePath resolves from $env:LOCALAPPDATA, so a test points it at a COPY by
# setting that variable -- the live token cache is never read.

function Import-SupervisorFunctionText([string]$Name) {
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

foreach ($fn in @(
        "Get-DevtunnelTokenCachePath",
        "Get-DevtunnelTokenCacheState",
        "Get-DevtunnelTokenCacheAlertLine",
        "Test-DevtunnelLoggedInCached")) {
    Invoke-Expression (Import-SupervisorFunctionText $fn)
}

# Stubs for the live-effect calls the gate makes.
function Write-Log($msg) {
    if (-not $script:LoggedLines) { $script:LoggedLines = New-Object System.Collections.ArrayList }
    [void]$script:LoggedLines.Add($msg)
}

function Test-DevtunnelLoggedIn {
    $script:CliCalls++
    return [bool]$script:StubLoggedInAnswer
}

# Script-scope state the real functions read; the Tests file resets these in BeforeEach.
$script:DevtunnelLoginCacheSeconds = 600
$script:DevtunnelLoginCachedUntil = [datetime]::MinValue
$script:DevtunnelLoginCacheUseLogged = $false
$script:DevtunnelLoginAnswerWasCached = $false
$script:DevtunnelLoginRecheckReason = "first check"
$script:DevtunnelTokenCacheZeroAlerted = $false
$script:CliCalls = 0
$script:StubLoggedInAnswer = $true
