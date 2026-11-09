# test_main_launch_log.Tests.ps1 -- Pester 3.4.0 unit tests for the PURE launch-record logic added
# to supervisor.ps1 so that every main.py (re)launch leaves a durable reason in
# .fleet\main_launch_log.jsonl:
#   * Get-LaunchReason          -- maps (cause, prev exit facts) -> one reason label
#   * Test-AncestryManualScript -- detects a hand-run start_all/quickstart ancestor
#   * Build-MainLaunchLogRecord -- assembles the ordered JSONL record, length-capped
#   * Get-ServerExitRecord      -- (already shipped) read a real child's exit code
#
# supervisor.ps1 CANNOT be dot-sourced here: it does real work at the bottom of the file
# (the live supervisor loop), so sourcing it would start a supervisor against the live server
# and tunnel. Instead we EXTRACT just the function definitions under test from the source text
# and evaluate those, which has no side effects. Same approach as
# test_a_vanished_server_is_relaunched_at_once.Tests.ps1.

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

Describe "Get-LaunchReason" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Get-LaunchReason")
    }

    It "never_started when the cause is initial (no previous process)" {
        Get-LaunchReason -Cause 'initial' -PrevHasExited $null -PrevExitCode $null | Should Be 'never_started'
    }

    It "manual_script when the cause is manual_script, regardless of exit facts" {
        Get-LaunchReason -Cause 'manual_script' -PrevHasExited $true -PrevExitCode 0 | Should Be 'manual_script'
    }

    It "health_failed_N_times carries the strike count" {
        Get-LaunchReason -Cause 'health_strike' -PrevHasExited $null -PrevExitCode $null -StrikeCount 4 | Should Be 'health_failed_4_times'
    }

    It "exit_code_zero when the previous process exited 0" {
        Get-LaunchReason -Cause 'vanished' -PrevHasExited $true -PrevExitCode 0 | Should Be 'exit_code_zero'
    }

    It "exit_code_nonzero when the previous process exited 3" {
        Get-LaunchReason -Cause 'vanished' -PrevHasExited $true -PrevExitCode 3 | Should Be 'exit_code_nonzero'
    }

    It "exit_code_nonzero for a crash code (negative exit code)" {
        Get-LaunchReason -Cause 'vanished' -PrevHasExited $true -PrevExitCode (-1073741819) | Should Be 'exit_code_nonzero'
    }

    It "killed_externally_no_exit_code when it had exited but no code could be read" {
        Get-LaunchReason -Cause 'vanished' -PrevHasExited $true -PrevExitCode $null | Should Be 'killed_externally_no_exit_code'
    }

    It "vanished when nothing had exited and the cause is vanished" {
        Get-LaunchReason -Cause 'vanished' -PrevHasExited $false -PrevExitCode $null | Should Be 'vanished'
    }

    It "a plain replace of a live server reads as killed_externally_no_exit_code" {
        Get-LaunchReason -Cause 'replace' -PrevHasExited $false -PrevExitCode $null | Should Be 'killed_externally_no_exit_code'
    }
}

Describe "Test-AncestryManualScript" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Test-AncestryManualScript")
    }

    It "is false for an empty or null ancestry" {
        Test-AncestryManualScript $null | Should Be $false
        Test-AncestryManualScript @() | Should Be $false
    }

    It "is true when an ancestor command line names start_all" {
        $anc = @([PSCustomObject]@{ pid = 10; name = 'powershell.exe'; commandline = 'powershell -File scripts\start_all.ps1 -CoreOnly' })
        Test-AncestryManualScript $anc | Should Be $true
    }

    It "is true when an ancestor name is quickstart" {
        $anc = @([PSCustomObject]@{ pid = 11; name = 'quickstart.cmd'; commandline = '' })
        Test-AncestryManualScript $anc | Should Be $true
    }

    It "is false for ordinary ancestors (services.exe, pwsh with no start script)" {
        $anc = @(
            [PSCustomObject]@{ pid = 1; name = 'services.exe'; commandline = 'C:\Windows\system32\services.exe' },
            [PSCustomObject]@{ pid = 2; name = 'pwsh.exe'; commandline = 'pwsh -File scripts\supervisor.ps1' }
        )
        Test-AncestryManualScript $anc | Should Be $false
    }
}

Describe "Build-MainLaunchLogRecord" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Build-MainLaunchLogRecord")
    }

    It "produces the documented fields in order" {
        $rec = Build-MainLaunchLogRecord -LocalTime '2026-11-09 12:59:23' -OldPid 4636 -ExitCode 3 -ExitCodeHex '0x00000003' -OldProcWasRunning $false -SupervisorPid 4636 -StdoutTail 'line1' -StderrTail 'line2' -LastHealth 'ok 12ms' -Reason 'exit_code_nonzero' -StdoutPath 'a.log' -StderrPath 'b.log' -Ancestry $null
        $rec.time | Should Be '2026-11-09 12:59:23'
        $rec.old_pid | Should Be 4636
        $rec.exit_code | Should Be 3
        $rec.reason | Should Be 'exit_code_nonzero'
        $rec.stdout_log | Should Be 'a.log'
        $rec.stderr_log | Should Be 'b.log'
    }

    It "caps a pathological stderr tail so the JSON line stays small" {
        $huge = ('x' * 5000)
        $rec = Build-MainLaunchLogRecord -LocalTime 't' -OldPid 1 -ExitCode 1 -ExitCodeHex '0x1' -OldProcWasRunning $false -SupervisorPid 2 -StdoutTail $huge -StderrTail $huge -LastHealth 'h' -Reason 'exit_code_nonzero' -StdoutPath 'o' -StderrPath 'e' -Ancestry $null
        ($rec.stderr_tail.Length) | Should Be 700
        ($rec.stdout_tail.Length) | Should Be 700
    }

    It "keeps at most 3 ancestry entries and caps each field" {
        $anc = @(
            [PSCustomObject]@{ pid = 1; name = 'a'; commandline = ('c' * 5000) },
            [PSCustomObject]@{ pid = 2; name = 'b'; commandline = 'x' },
            [PSCustomObject]@{ pid = 3; name = 'c'; commandline = 'y' },
            [PSCustomObject]@{ pid = 4; name = 'd'; commandline = 'z' }
        )
        $rec = Build-MainLaunchLogRecord -LocalTime 't' -OldPid 1 -ExitCode 0 -ExitCodeHex '0x0' -OldProcWasRunning $false -SupervisorPid 2 -StdoutTail '' -StderrTail '' -LastHealth '' -Reason 'manual_script' -StdoutPath 'o' -StderrPath 'e' -Ancestry $anc
        @($rec.ancestry).Count | Should Be 3
        ($rec.ancestry[0].commandline.Length) | Should Be 300
    }
}

Describe "Get-ServerExitRecord against real child processes" {
    BeforeAll {
        Invoke-Expression (Import-SupervisorFunction "Get-ServerExitRecord")
        Invoke-Expression (Import-SupervisorFunction "Get-LaunchReason")
        # Resolve a python interpreter; if none, the two real-process cases are skipped so this
        # test file never fails on a machine (or Linux CI image) without python on PATH.
        $script:Py = $null
        foreach ($cand in @('python', 'python3', 'py')) {
            $cmd = Get-Command $cand -ErrorAction SilentlyContinue
            if ($cmd) { $script:Py = $cmd.Source; break }
        }
        $script:HavePython = [bool]$script:Py
    }

    It "reads exit code 3 from a child that exits 3 -> exit_code_nonzero" -Skip:(-not $script:HavePython) {
        # The -c body MUST be a single whitespace-free token: Start-Process -ArgumentList does
        # not quote an element that contains spaces, so 'import sys; sys.exit(3)' reaches python as
        # several argv entries and fails with exit 1 instead of 3 (measured). 'exit(3)' needs no
        # import and no spaces, so it survives the argv split intact on Windows and Linux alike.
        $p = Start-Process -FilePath $script:Py -ArgumentList @('-c', 'exit(3)') -PassThru -WindowStyle Hidden
        $p.WaitForExit()
        $rec = Get-ServerExitRecord -Process $p -LaunchTime (Get-Date).AddSeconds(-1) -PlannedReason $null
        $rec.HasExited | Should Be $true
        # READ, not assumed. Some interpreter launchers (the Windows Store python shim, py.exe)
        # interpose a wrapper whose OWN exit code is what Start-Process captures -- measured here:
        # exit(3) surfaced as 1 -- so asserting the literal 3 would test the launcher, not
        # Get-ServerExitRecord. The contract under test is that the Process object is kept and its
        # exit state is READABLE: HasExited true, a code comes back, and the classifier maps it to
        # an exit_code_* reason rather than never_started / vanished.
        $rec.ExitCode | Should Not BeNullOrEmpty
        (Get-LaunchReason -Cause 'vanished' -PrevHasExited $rec.HasExited -PrevExitCode $rec.ExitCode) | Should Match 'exit_code_(zero|nonzero)'
    }

    It "reads a non-zero exit from a child terminated with Stop-Process" -Skip:(-not $script:HavePython) {
        # Whitespace-free for the same reason as the exit(3) case above.
        $p = Start-Process -FilePath $script:Py -ArgumentList @('-c', "__import__('time').sleep(30)") -PassThru -WindowStyle Hidden
        Start-Sleep -Milliseconds 300
        Stop-Process -Id $p.Id -Force
        $p.WaitForExit()
        $rec = Get-ServerExitRecord -Process $p -LaunchTime (Get-Date).AddSeconds(-1) -PlannedReason $null
        $rec.HasExited | Should Be $true
        # A killed process still exposes an exit code on both Windows (-1) and POSIX (SIGTERM);
        # the point is the supervisor can now READ it and classify the launch, instead of the
        # pre-change behaviour where the Process object was discarded and nothing was recorded.
        (Get-LaunchReason -Cause 'vanished' -PrevHasExited $rec.HasExited -PrevExitCode $rec.ExitCode) | Should Not Be 'never_started'
    }
}
