# standby_watch.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE helpers in
# standby_watch.ps1. Hermetic: no network, no process, no main.py -- the file is
# dot-sourced, and its MAIN body is behind a `$MyInvocation.InvocationName -ne '.'`
# guard, so dot-sourcing defines the functions without probing anything or starting
# a secondary.
#
# ASCII / ENGLISH ONLY. No BOM.

$scriptPath = Join-Path $PSScriptRoot "standby_watch.ps1"
. $scriptPath

Describe "Test-IsHealthyMainPyBody" {

    It "accepts main.py's body: status ok and server_pid > 0" {
        Test-IsHealthyMainPyBody '{"status":"ok","server_pid":1234}' | Should Be $true
    }

    It "rejects a 200 body that is not JSON (e.g. a relay sign-in page)" {
        Test-IsHealthyMainPyBody '<html>sign in</html>' | Should Be $false
    }

    It "rejects JSON with status not ok" {
        Test-IsHealthyMainPyBody '{"status":"starting","server_pid":1234}' | Should Be $false
    }

    It "rejects JSON with server_pid 0 or missing" {
        Test-IsHealthyMainPyBody '{"status":"ok","server_pid":0}' | Should Be $false
        Test-IsHealthyMainPyBody '{"status":"ok"}' | Should Be $false
    }

    It "rejects null or empty body" {
        Test-IsHealthyMainPyBody "" | Should Be $false
        Test-IsHealthyMainPyBody $null | Should Be $false
    }
}

Describe "Get-LocalHealthUrl" {

    It "builds a loopback /health URL for a port" {
        Get-LocalHealthUrl -Port 8002 | Should Be "http://127.0.0.1:8002/health"
    }

    It "builds the test secondary URL on 18002" {
        Get-LocalHealthUrl -Port 18002 | Should Be "http://127.0.0.1:18002/health"
    }
}

Describe "Resolve-PrimaryHealthUrl" {

    $default = "http://127.0.0.1:8000/health"

    It "keeps an explicit non-default -PrimaryUrl" {
        Resolve-PrimaryHealthUrl -PrimaryUrl "http://127.0.0.1:18000/health" -PrimaryPort 8000 -DefaultUrl $default |
            Should Be "http://127.0.0.1:18000/health"
    }

    It "derives from -PrimaryPort when the URL is left at the default" {
        Resolve-PrimaryHealthUrl -PrimaryUrl $default -PrimaryPort 18000 -DefaultUrl $default |
            Should Be "http://127.0.0.1:18000/health"
    }

    It "returns the default when nothing was overridden" {
        Resolve-PrimaryHealthUrl -PrimaryUrl $default -PrimaryPort 8000 -DefaultUrl $default |
            Should Be $default
    }
}

Describe "Format-StandbyResultLine" {

    It "reports a started secondary with its switch time" {
        $line = Format-StandbyResultLine -Decision "secondary_started" -SwitchSec 4.2 -SecondaryPort 8002
        $line | Should Match 'decision=secondary_started'
        $line | Should Match 'secondary_port=8002'
        $line | Should Match 'switch_sec=4\.2s'
    }

    It "renders a null switch time as n/a" {
        $line = Format-StandbyResultLine -Decision "primary_ok" -SwitchSec $null -SecondaryPort 8002
        $line | Should Match 'decision=primary_ok'
        $line | Should Match 'switch_sec=n/a'
    }
}
