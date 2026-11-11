# public_url_probe.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE helpers in
# public_url_probe.ps1 (alert title/body per cause, and the re-alert debounce). The
# classification itself is covered in CI by tools/test_tunnel_probe_*.py (Linux/pytest);
# these cover the PowerShell-only presentation + cadence that do not live in Python.
#
# public_url_probe.ps1 starts its measurement loop as soon as it is invoked directly, so it
# cannot be `& `-ed here. Dot-sourcing defines its functions WITHOUT starting the loop, because
# the script gates the bottom-of-file invocation on `$MyInvocation.InvocationName -ne '.'`.
#
# NOTE: this Pester suite is NOT run in CI (CI is Linux/pytest only, by design -- see
# .github/workflows/ci.yml). It is the local-machine check for the PowerShell helpers.

$scriptPath = Join-Path $PSScriptRoot "public_url_probe.ps1"
. $scriptPath

Describe "Get-AlertTitleForCause" {
    BeforeAll { . (Join-Path $PSScriptRoot "public_url_probe.ps1") }

    It "names the sign-in cause" {
        (Get-AlertTitleForCause 'not_logged_in') | Should Match 'SIGNED IN'
    }
    It "names the no-host cause" {
        (Get-AlertTitleForCause 'no_host') | Should Match 'NO HOST'
    }
    It "names the local-server cause" {
        (Get-AlertTitleForCause 'local_server_down') | Should Match 'LOCAL'
    }
    It "names the public-path cause" {
        (Get-AlertTitleForCause 'public_path_down') | Should Match 'PUBLIC'
    }
    It "falls back rather than throwing on an unknown code" {
        (Get-AlertTitleForCause 'something_new') | Should Not BeNullOrEmpty
    }
}

Describe "Get-AlertBodyForCause" {
    BeforeAll { . (Join-Path $PSScriptRoot "public_url_probe.ps1") }

    It "tells the sign-in fix to use a VISIBLE console and NOT device-code" {
        $b = Get-AlertBodyForCause -Code 'not_logged_in' -Detail 'not signed in' -PublicUrl 'https://x/health' -FailedForSec 42
        $b | Should Match 'devtunnel login'
        $b | Should Match 'VISIBLE console'
        $b | Should Match 'device-code'
    }
    It "includes how long it has been failing" {
        $b = Get-AlertBodyForCause -Code 'public_path_down' -Detail 'relay down' -PublicUrl 'https://x/health' -FailedForSec 90
        $b | Should Match '90s'
    }
    It "includes the public url" {
        $b = Get-AlertBodyForCause -Code 'no_host' -Detail 'zero' -PublicUrl 'https://host.jpe1.devtunnels.ms/health' -FailedForSec 10
        $b | Should Match 'devtunnels.ms'
    }
}

Describe "Test-ShouldAlertNow" {
    BeforeAll { . (Join-Path $PSScriptRoot "public_url_probe.ps1") }

    It "never alerts when healthy" {
        (Test-ShouldAlertNow -Healthy $true -Code 'ok' -LastAlertedCode '' -LastAlertSec 0 -NowSec 100 -RepeatSec 300) | Should Be $false
    }
    It "alerts at once on a brand-new failure" {
        (Test-ShouldAlertNow -Healthy $false -Code 'not_logged_in' -LastAlertedCode '' -LastAlertSec 0 -NowSec 100 -RepeatSec 300) | Should Be $true
    }
    It "alerts at once when the cause CHANGES" {
        (Test-ShouldAlertNow -Healthy $false -Code 'public_path_down' -LastAlertedCode 'not_logged_in' -LastAlertSec 95 -NowSec 100 -RepeatSec 300) | Should Be $true
    }
    It "does NOT re-alert the same cause before RepeatSec" {
        (Test-ShouldAlertNow -Healthy $false -Code 'no_host' -LastAlertedCode 'no_host' -LastAlertSec 100 -NowSec 200 -RepeatSec 300) | Should Be $false
    }
    It "re-alerts the same cause after RepeatSec" {
        (Test-ShouldAlertNow -Healthy $false -Code 'no_host' -LastAlertedCode 'no_host' -LastAlertSec 100 -NowSec 410 -RepeatSec 300) | Should Be $true
    }
}
