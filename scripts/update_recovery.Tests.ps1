# update_recovery.Tests.ps1 -- Pester 3.4.0 unit and disposable-repository
# integration tests for start_all.ps1's force-push recovery.

Describe "Get-UpdateStrategy" {
    BeforeAll {
        . (Join-Path $PSScriptRoot "update_recovery.ps1")
    }

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

Describe "Invoke-RewrittenUpstreamRecovery integration" {
    BeforeAll {
        . (Join-Path $PSScriptRoot "update_recovery.ps1")

        function Set-TestFile([string]$Path, [string]$Text) {
            $parent = Split-Path -Parent $Path
            if ($parent -and -not (Test-Path $parent)) {
                New-Item -ItemType Directory -Force -Path $parent | Out-Null
            }
            [System.IO.File]::WriteAllText(
                $Path,
                $Text,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }

        function Initialize-TestRepo([string]$Path) {
            New-Item -ItemType Directory -Force -Path $Path | Out-Null
            & git init --quiet $Path
            & git -C $Path config user.name "Update Recovery Test"
            & git -C $Path config user.email "update-recovery@example.invalid"
        }
    }

    BeforeEach {
        # Pester 3 keeps TestDrive for the whole Describe, not each It.
        $caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString("N"))
        $remote = Join-Path $caseRoot "remote.git"
        $oldSource = Join-Path $caseRoot "old-source"
        $rewriteSource = Join-Path $caseRoot "rewrite-source"
        $client = Join-Path $caseRoot "client"

        & git init --quiet --bare $remote

        Initialize-TestRepo $oldSource
        Set-TestFile (Join-Path $oldSource "version.txt") "old-base"
        & git -C $oldSource add version.txt
        & git -C $oldSource commit --quiet -m "old base"
        Set-TestFile (Join-Path $oldSource "version.txt") "old-tip"
        & git -C $oldSource commit --quiet -am "old tip"
        & git -C $oldSource branch -M main
        & git -C $oldSource remote add origin $remote
        & git -C $oldSource push --quiet -u origin main
        & git --git-dir=$remote symbolic-ref HEAD refs/heads/main

        & git clone --quiet $remote $client
        & git -C $client config user.name "Update Recovery Client"
        & git -C $client config user.email "update-client@example.invalid"
        $oldClientSha = (& git -C $client rev-parse HEAD).Trim()

        # Model a metadata scrub: an unrelated clean history replaces remote
        # main and then gains a newer commit.
        Initialize-TestRepo $rewriteSource
        Set-TestFile (Join-Path $rewriteSource "version.txt") "clean-base"
        & git -C $rewriteSource add version.txt
        & git -C $rewriteSource commit --quiet -m "clean rewritten base"
        Set-TestFile (Join-Path $rewriteSource "version.txt") "clean-latest"
        & git -C $rewriteSource commit --quiet -am "clean latest"
        & git -C $rewriteSource branch -M main
        & git -C $rewriteSource remote add origin $remote
        & git -C $rewriteSource push --quiet --force origin main

        & git -C $client fetch --quiet origin
    }

    It "recovers the real non-fast-forward shape and retains every local change" {
        Set-TestFile (Join-Path $client "version.txt") "local tracked edit"
        Set-TestFile (Join-Path $client "local-note.txt") "local untracked note"

        $behind = [int]((& git -C $client rev-list --count "HEAD..@{u}").Trim())
        $ahead = [int]((& git -C $client rev-list --count "@{u}..HEAD").Trim())
        & git -C $client merge-base --is-ancestor HEAD "@{u}"
        $canFF = ($LASTEXITCODE -eq 0)

        Get-UpdateStrategy -Behind $behind -Ahead $ahead -CanFastForward $canFF |
            Should Be "rewritten-upstream"
        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "@{u}" -Timestamp "20260725-120000"

        $result.Success | Should Be $true
        (& git -C $client rev-parse HEAD).Trim() |
            Should Be (& git -C $client rev-parse "@{u}").Trim()
        (& git -C $client rev-parse $result.BackupBranch).Trim() | Should Be $oldClientSha
        $result.StashCreated | Should Be $true
        $stashedPaths = @(& git -C $client stash show --include-untracked --name-only $result.StashRef)
        ($stashedPaths -contains "version.txt") | Should Be $true
        ($stashedPaths -contains "local-note.txt") | Should Be $true
        @(& git -C $client status --porcelain).Count | Should Be 0
        (Get-Content (Join-Path $client "version.txt") -Raw).Trim() | Should Be "clean-latest"
    }

    It "rolls back to the original checkout if the requested reset target fails" {
        Set-TestFile (Join-Path $client "version.txt") "tracked work to restore"
        Set-TestFile (Join-Path $client "untracked-work.txt") "untracked work to restore"

        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "refs/remotes/origin/does-not-exist" `
            -Timestamp "20260725-120001"

        $result.Success | Should Be $false
        $result.Error | Should Match "reset --hard"
        (& git -C $client rev-parse HEAD).Trim() | Should Be $oldClientSha
        (Get-Content (Join-Path $client "version.txt") -Raw).Trim() |
            Should Be "tracked work to restore"
        (Get-Content (Join-Path $client "untracked-work.txt") -Raw).Trim() |
            Should Be "untracked work to restore"
        $result.StashCreated | Should Be $true
        (& git -C $client rev-parse $result.StashRef).Trim() | Should Be $result.StashRef
    }
}
