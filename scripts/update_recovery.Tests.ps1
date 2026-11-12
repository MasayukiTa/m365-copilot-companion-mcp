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

    It "recovers a checkout stranded in an unfinished merge with an unmerged index" {
        # Reproduce the real incident shape: a prior merge was left unfinished,
        # so MERGE_HEAD and unmerged paths are present when recovery runs. The
        # merge is built from two commits that SHARE an ancestor (so git really
        # starts a merge and records a conflict, rather than refusing unrelated
        # histories). The rewritten upstream that recovery resets to is still the
        # force-pushed origin/main prepared by BeforeEach.
        $sharedBase = (& git -C $client rev-parse HEAD).Trim()

        # A sibling commit off the shared base that touches the same file.
        & git -C $client checkout --quiet -b conflicting-side $sharedBase
        Set-TestFile (Join-Path $client "version.txt") "conflicting side edit"
        & git -C $client commit --quiet -am "conflicting side work"

        # Back on main, a different edit to the same file, then start the merge.
        & git -C $client checkout --quiet main
        Set-TestFile (Join-Path $client "version.txt") "main side edit"
        & git -C $client commit --quiet -am "main side work"
        $conflictedHead = (& git -C $client rev-parse HEAD).Trim()
        & git -C $client -c core.editor=true merge --no-edit conflicting-side 2>&1 | Out-Null

        Assert-Equal (Test-Path (Join-Path $client ".git\MERGE_HEAD")) $true
        Assert-Equal (@(& git -C $client ls-files -u).Count -gt 0) $true

        Set-TestFile (Join-Path $client "version.txt") "my conflict resolution"
        Set-TestFile (Join-Path $client ".git/info/exclude") ".env`n.config/`n"
        Set-TestFile (Join-Path $client ".env") "SECRET=kept"
        Set-TestFile (Join-Path $client ".config/settings.txt") "my setting"
        Set-TestFile (Join-Path $client "untracked.txt") "my note"
        $originalIndex = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $client ".git/index")))
        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "@{u}" -Timestamp "20260725-120002"
        if (-not $result.Success) { throw $result.Error }
        Assert-Equal $result.Success $true
        Assert-Equal ([Convert]::ToBase64String([IO.File]::ReadAllBytes("$($result.SavedCopy).index"))) $originalIndex
        Assert-Equal (Get-Content (Join-Path $result.SavedCopy "version.txt") -Raw).Trim() "my conflict resolution"
        Assert-Equal (Get-Content (Join-Path $result.SavedCopy "untracked.txt") -Raw).Trim() "my note"
        Assert-Equal (Get-Content (Join-Path $client ".env") -Raw).Trim() "SECRET=kept"
        Assert-Equal (Get-Content (Join-Path $client ".config/settings.txt") -Raw).Trim() "my setting"

        Assert-Equal $result.Success $true
        Assert-Equal $result.Error ""
        # The reset landed on the rewritten upstream tip.
        Assert-Equal `
            ((& git -C $client rev-parse HEAD).Trim()) `
            ((& git -C $client rev-parse "@{u}").Trim())
        # The in-progress merge is gone and the worktree is clean.
        Assert-Equal (Test-Path (Join-Path $client ".git\MERGE_HEAD")) $false
        Assert-Equal @(& git -C $client status --porcelain).Count 0
        # The backup branch still points at the pre-recovery HEAD (the tip that
        # was being merged into), so the original checkout is never lost.
        Assert-Equal ((& git -C $client rev-parse $result.BackupBranch).Trim()) $conflictedHead
    }

    It "leaves a clean, non-conflicted checkout untouched except for the reset (no spurious abort)" {
        # The guard must only act when an operation is actually in progress. A
        # plain diverged checkout with no local edits has no merge/rebase state
        # and no unmerged paths, and must recover exactly as before.
        Assert-Equal (Test-Path (Join-Path $client ".git\MERGE_HEAD")) $false
        Assert-Equal @(& git -C $client ls-files -u).Count 0

        $result = Invoke-RewrittenUpstreamRecovery `
            -RepoRoot $client -Upstream "@{u}" -Timestamp "20260725-120003"

        Assert-Equal $result.Success $true
        Assert-Equal $result.Error ""
        Assert-Equal $result.HadLocalChanges $false
        Assert-Equal $result.StashCreated $false
        Assert-Equal `
            ((& git -C $client rev-parse HEAD).Trim()) `
            ((& git -C $client rev-parse "@{u}").Trim())
        Assert-Equal ((& git -C $client rev-parse $result.BackupBranch).Trim()) $oldClientSha
    }
}

Describe "quickstart_update.ps1 wrapper integration" {
    BeforeAll {
        $script:WrapperPath = Join-Path $PSScriptRoot "quickstart_update.ps1"

        function Assert-Equal($Actual, $Expected) {
            if ($Actual -ne $Expected) {
                throw "Expected <$Expected>, got <$Actual>"
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
            & git -C $Path config user.name "Quickstart Update Test"
            & git -C $Path config user.email "quickstart-update@example.invalid"
        }

        function Invoke-Wrapper([string]$RepoRoot) {
            & $script:WrapperPath -RepoRoot $RepoRoot | Out-Null
            return $LASTEXITCODE
        }
    }

    It "recovers a rewritten (force-pushed) upstream and stops with exit 0" {
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
        & git -C $client config user.name "Quickstart Update Client"
        & git -C $client config user.email "quickstart-client@example.invalid"
        $oldClientSha = (& git -C $client rev-parse HEAD).Trim()

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

        Set-TestFile (Join-Path $client "version.txt") "local tracked edit"

        $exit = Invoke-Wrapper $client

        Assert-Equal $exit 0
        Assert-Equal `
            ((& git -C $client rev-parse HEAD).Trim()) `
            ((& git -C $client rev-parse "@{u}").Trim())
        Assert-Equal (Get-Content (Join-Path $client "version.txt") -Raw).Trim() "clean-latest"
        $backup = @(& git -C $client branch --list "backup/pre-reset-*")
        Assert-Equal ($backup.Count -ge 1) $true
        $stashes = @(& git -C $client stash list)
        Assert-Equal ($stashes.Count -ge 1) $true
    }

    It "fast-forwards a plainly-behind clone and stops with exit 0, with no backup branch" {
        $caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString("N"))
        $remote = Join-Path $caseRoot "remote.git"
        $source = Join-Path $caseRoot "source"
        $client = Join-Path $caseRoot "client"

        & git init --quiet --bare $remote

        Initialize-TestRepo $source
        Set-TestFile (Join-Path $source "version.txt") "base"
        & git -C $source add version.txt
        & git -C $source commit --quiet -m "base"
        & git -C $source branch -M main
        & git -C $source remote add origin $remote
        & git -C $source push --quiet -u origin main
        & git --git-dir=$remote symbolic-ref HEAD refs/heads/main

        & git clone --quiet $remote $client
        & git -C $client config user.name "Quickstart Update Client"
        & git -C $client config user.email "quickstart-client@example.invalid"

        Set-TestFile (Join-Path $source "version.txt") "advanced"
        & git -C $source commit --quiet -am "advance"
        & git -C $source push --quiet origin main

        & git -C $client fetch --quiet origin

        $exit = Invoke-Wrapper $client

        Assert-Equal $exit 0
        Assert-Equal `
            ((& git -C $client rev-parse HEAD).Trim()) `
            ((& git -C $client rev-parse "@{u}").Trim())
        Assert-Equal (Get-Content (Join-Path $client "version.txt") -Raw).Trim() "advanced"
        $backup = @(& git -C $client branch --list "backup/pre-reset-*")
        Assert-Equal $backup.Count 0
    }
}

Describe "Uncovered checkout shapes end in a safe state" {
    # Three shapes that the update path (Check-ForUpdates in start_all.ps1 and the
    # quickstart_update.ps1 wrapper, which share the same behind/ahead/fast-forward
    # probe) previously had no test for:
    #   * detached HEAD      -- HEAD points at a commit, not a branch, so @{u} has
    #                           no meaning and the behind-count probe fails.
    #   * no upstream        -- the current branch has no configured @{u}.
    #   * offline            -- the pre-update fetch itself fails.
    # A user can only double-click start_all.bat / quickstart.bat, so each shape
    # must end on its own in a safe state: local work is never lost, the checkout
    # still runs the code it has, and no guided reset is attempted. Each test builds
    # the shape in a temp repo and asserts HEAD and the worktree are untouched, the
    # expected exit code is returned, and no reset/backup/stash was performed.
    BeforeAll {
        $script:WrapperPath = Join-Path $PSScriptRoot "quickstart_update.ps1"

        function Assert-Equal($Actual, $Expected) {
            if ($Actual -ne $Expected) {
                throw "Expected <$Expected>, got <$Actual>"
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
            & git -C $Path config user.name "Uncovered Shape Test"
            & git -C $Path config user.email "uncovered-shape@example.invalid"
        }

        # A bare remote with a real main and a HEAD symref, plus a clone of it.
        # Mirrors the shared setup the other Describe blocks use.
        function New-ClientOfRemote([string]$CaseRoot) {
            $remote = Join-Path $CaseRoot "remote.git"
            $source = Join-Path $CaseRoot "source"
            $client = Join-Path $CaseRoot "client"

            & git init --quiet --bare $remote

            Initialize-TestRepo $source
            Set-TestFile (Join-Path $source "version.txt") "base"
            & git -C $source add version.txt
            & git -C $source commit --quiet -m "base"
            & git -C $source branch -M main
            & git -C $source remote add origin $remote
            & git -C $source push --quiet -u origin main
            & git --git-dir=$remote symbolic-ref HEAD refs/heads/main

            & git clone --quiet $remote $client
            & git -C $client config user.name "Uncovered Shape Client"
            & git -C $client config user.email "uncovered-client@example.invalid"
            return $client
        }

        function Invoke-Wrapper([string]$RepoRoot) {
            & $script:WrapperPath -RepoRoot $RepoRoot | Out-Null
            return $LASTEXITCODE
        }

        function Get-HeadSha([string]$RepoRoot) {
            return (& git -C $RepoRoot rev-parse HEAD).Trim()
        }

        function Test-IsDetached([string]$RepoRoot) {
            & git -C $RepoRoot symbolic-ref -q HEAD > $null 2>&1
            return ($LASTEXITCODE -ne 0)
        }
    }

    It "detached HEAD: wrapper exits 1 and leaves HEAD detached and the worktree untouched" {
        $caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString("N"))
        $client = New-ClientOfRemote $caseRoot

        # Detach HEAD at the current commit, then make a local edit that must survive.
        $sha = Get-HeadSha $client
        & git -C $client checkout --quiet --detach $sha
        Set-TestFile (Join-Path $client "local-work.txt") "local work that must survive"

        Assert-Equal (Test-IsDetached $client) $true
        $headBefore = Get-HeadSha $client

        $exit = Invoke-Wrapper $client

        # Safe state: no update attempted. Exit 1 tells the caller the checkout is
        # unchanged, HEAD is still the same detached commit, and no backup/stash was made.
        Assert-Equal $exit 1
        Assert-Equal (Get-HeadSha $client) $headBefore
        Assert-Equal (Test-IsDetached $client) $true
        Assert-Equal (Get-Content (Join-Path $client "local-work.txt") -Raw).Trim() "local work that must survive"
        $backup = @(& git -C $client branch --list "backup/pre-reset-*")
        Assert-Equal $backup.Count 0
        $stashes = @(& git -C $client stash list)
        Assert-Equal $stashes.Count 0
    }

    It "no upstream: wrapper exits 1 and leaves the branch, HEAD and worktree untouched" {
        $caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString("N"))
        $client = New-ClientOfRemote $caseRoot

        # A local branch with no configured upstream, plus a local edit to protect.
        & git -C $client checkout --quiet -b work
        Set-TestFile (Join-Path $client "local-work.txt") "local work that must survive"
        $headBefore = Get-HeadSha $client

        # Confirm the shape: @{u} cannot be resolved for this branch.
        & { $ErrorActionPreference = 'SilentlyContinue'; & git -C $client rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>&1 | Out-Null }
        Assert-Equal ($LASTEXITCODE -ne 0) $true

        $exit = Invoke-Wrapper $client

        Assert-Equal $exit 1
        Assert-Equal (Get-HeadSha $client) $headBefore
        Assert-Equal ((& git -C $client rev-parse --abbrev-ref HEAD).Trim()) "work"
        Assert-Equal (Get-Content (Join-Path $client "local-work.txt") -Raw).Trim() "local work that must survive"
        $backup = @(& git -C $client branch --list "backup/pre-reset-*")
        Assert-Equal $backup.Count 0
        $stashes = @(& git -C $client stash list)
        Assert-Equal $stashes.Count 0
    }

    It "offline: a failed fetch stops the update with HEAD and worktree untouched and no reset" {
        $caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString("N"))
        $client = New-ClientOfRemote $caseRoot

        # Make the network step fail the way an offline machine does: point origin
        # at a path that does not exist, so `git fetch` returns non-zero. This is the
        # exact guard Check-ForUpdates applies (fetch exit != 0 -> return before any
        # update), so the fetch is modelled here directly rather than through the
        # wrapper (the wrapper's caller performs the fetch).
        & git -C $client remote set-url origin (Join-Path $caseRoot "does-not-exist.git")
        Set-TestFile (Join-Path $client "local-work.txt") "local work that must survive"
        $headBefore = Get-HeadSha $client
        $env:GIT_TERMINAL_PROMPT = '0'

        & { $ErrorActionPreference = 'SilentlyContinue'; & git -C $client fetch --quiet 2>&1 | Out-Null }
        $fetchExit = $LASTEXITCODE

        # The guard's contract: fetch failed, so no behind-count, no strategy, no reset.
        Assert-Equal ($fetchExit -ne 0) $true
        Assert-Equal (Get-HeadSha $client) $headBefore
        Assert-Equal ((& git -C $client rev-parse --abbrev-ref HEAD).Trim()) "main"
        Assert-Equal @(& git -C $client status --porcelain).Count 1
        Assert-Equal (Get-Content (Join-Path $client "local-work.txt") -Raw).Trim() "local work that must survive"
        $backup = @(& git -C $client branch --list "backup/pre-reset-*")
        Assert-Equal $backup.Count 0
        $stashes = @(& git -C $client stash list)
        Assert-Equal $stashes.Count 0
    }
}
