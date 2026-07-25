# update_recovery.Tests.ps1 -- Pester 3.4.0 unit and disposable-repository
# integration tests for start_all.ps1's force-push recovery.

Describe "Get-UpdateStrategy" {
    BeforeAll {
        . (Join-Path $PSScriptRoot "update_recovery.ps1")
        function Assert-Equal($Actual, $Expected) {
            if ($Actual -ne $Expected) {
                throw "Expected <$Expected>, got <$Actual>"
            }
        }
    }

    It "up to date: Behind 0 -> up-to-date" {
        Assert-Equal (Get-UpdateStrategy -Behind 0 -Ahead 0 -CanFastForward $false) "up-to-date"
    }

    It "already caught up (negative Behind, e.g. a parse quirk) -> up-to-date" {
        Assert-Equal (Get-UpdateStrategy -Behind -1 -Ahead 0 -CanFastForward $false) "up-to-date"
    }

    It "plain fast-forward: Behind 5, Ahead 0, CanFastForward true -> fast-forward" {
        Assert-Equal (Get-UpdateStrategy -Behind 5 -Ahead 0 -CanFastForward $true) "fast-forward"
    }

    It "the real incident shape: Behind 10, Ahead 4, CanFastForward false -> rewritten-upstream" {
        Assert-Equal (Get-UpdateStrategy -Behind 10 -Ahead 4 -CanFastForward $false) "rewritten-upstream"
    }

    It "diverged with Ahead 0 and CanFastForward false -> diverged-unknown" {
        Assert-Equal (Get-UpdateStrategy -Behind 3 -Ahead 0 -CanFastForward $false) "diverged-unknown"
    }

    It "fast-forward wins even if Ahead is also (nonsensically) positive" {
        Assert-Equal (Get-UpdateStrategy -Behind 2 -Ahead 1 -CanFastForward $true) "fast-forward"
    }

    It "nonsense/negative Ahead with Behind>0 and not fast-forwardable -> still returns a valid value" {
        # Pester 3.4's "Should Contain" checks file CONTENTS, not array membership --
        # use PowerShell's own -contains operator against the known-valid result set.
        $result = Get-UpdateStrategy -Behind 7 -Ahead -2 -CanFastForward $false
        $validValues = @("up-to-date", "fast-forward", "rewritten-upstream", "diverged-unknown")
        Assert-Equal ($validValues -contains $result) $true
    }
}

Describe "Invoke-RewrittenUpstreamRecovery integration" {
    BeforeAll {
        . (Join-Path $PSScriptRoot "update_recovery.ps1")

        function Assert-Equal($Actual, $Expected) {
            if ($Actual -ne $Expected) {
                throw "Expected <$Expected>, got <$Actual>"
            }
        }

        function Assert-Matches([string]$Actual, [string]$Pattern) {
            if ($Actual -notmatch $Pattern) {
                throw "Expected <$Actual> to match <$Pattern>"
            }
        }

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

        Assert-Equal `
            (Get-UpdateStrategy -Behind $behind -Ahead $ahead -CanFastForward $canFF) `
            "rewritten-upstream"
        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "@{u}" -Timestamp "20260725-120000"

        Assert-Equal $result.Success $true
        Assert-Equal `
            ((& git -C $client rev-parse HEAD).Trim()) `
            ((& git -C $client rev-parse "@{u}").Trim())
        Assert-Equal ((& git -C $client rev-parse $result.BackupBranch).Trim()) $oldClientSha
        Assert-Equal $result.StashCreated $true
        $stashedPaths = @(& git -C $client stash show --include-untracked --name-only $result.StashRef)
        Assert-Equal ($stashedPaths -contains "version.txt") $true
        Assert-Equal ($stashedPaths -contains "local-note.txt") $true
        Assert-Equal @(& git -C $client status --porcelain).Count 0
        Assert-Equal (Get-Content (Join-Path $client "version.txt") -Raw).Trim() "clean-latest"
    }

    It "rolls back to the original checkout if the requested reset target fails" {
        Set-TestFile (Join-Path $client "version.txt") "tracked work to restore"
        Set-TestFile (Join-Path $client "untracked-work.txt") "untracked work to restore"

        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "refs/remotes/origin/does-not-exist" `
            -Timestamp "20260725-120001"

        Assert-Equal $result.Success $false
        Assert-Matches $result.Error "reset --hard"
        Assert-Equal ((& git -C $client rev-parse HEAD).Trim()) $oldClientSha
        Assert-Equal `
            (Get-Content (Join-Path $client "version.txt") -Raw).Trim() `
            "tracked work to restore"
        Assert-Equal `
            (Get-Content (Join-Path $client "untracked-work.txt") -Raw).Trim() `
            "untracked work to restore"
        Assert-Equal $result.StashCreated $true
        Assert-Equal ((& git -C $client rev-parse $result.StashRef).Trim()) $result.StashRef
    }
}
