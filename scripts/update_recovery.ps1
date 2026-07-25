# update_recovery.ps1 -- side-effect-free definitions used by start_all.ps1.
#
# This file deliberately performs no work when dot-sourced.  Keeping the Git
# recovery operation here makes it possible to exercise the real force-push
# path against disposable repositories without launching the desktop stack.

function Get-UpdateStrategy {
    param(
        [int]$Behind,
        [int]$Ahead,
        [bool]$CanFastForward
    )

    if ($Behind -le 0) { return "up-to-date" }
    if ($CanFastForward) { return "fast-forward" }
    if ($Ahead -gt 0) { return "rewritten-upstream" }
    return "diverged-unknown"
}

function Invoke-GitResetQuiet {
    # Run the one Git operation for which a non-zero exit is an expected,
    # explicitly handled outcome without publishing native stderr into the
    # caller's PowerShell error stream. Pester 5 promotes native stderr to an
    # ErrorRecord; the hidden production launcher also has nowhere useful to
    # display it. The exit code remains authoritative.
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [string]$Target
    )

    if ($Target -match '["\r\n]') { return 2 }

    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "git"
        $psi.WorkingDirectory = $RepoRoot
        $psi.Arguments = 'reset --hard "' + $Target + '"'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $psi
        [void]$process.Start()
        # Drain both redirected streams before WaitForExit to avoid a full pipe
        # blocking the child process.
        [void]$process.StandardOutput.ReadToEnd()
        [void]$process.StandardError.ReadToEnd()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
        $process.Dispose()
        return $exitCode
    } catch {
        return 1
    }
}

function Invoke-RewrittenUpstreamRecovery {
    <#
    .SYNOPSIS
    Safely moves a diverged checkout to its rewritten upstream.

    .DESCRIPTION
    The current HEAD is retained by a timestamped local backup branch. Any
    tracked or untracked worktree changes are retained in a stash. Only after
    both safeguards succeed is the checkout reset to the requested upstream.

    The function never drops the backup branch or stash. If reset fails after
    stashing, it restores the original HEAD and reapplies the stash on a
    best-effort basis before returning failure.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [string]$Upstream = "@{u}",

        [string]$Timestamp = ""
    )

    $result = [ordered]@{
        Success = $false
        Error = ""
        OldSha = ""
        BackupBranch = ""
        HadLocalChanges = $false
        StashCreated = $false
        StashRef = ""
    }

    if (-not $Timestamp) {
        $Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    }

    $oldShaRaw = @(& git -C $RepoRoot rev-parse HEAD 2>$null)
    $oldShaExit = $LASTEXITCODE
    $oldSha = ($oldShaRaw | Select-Object -First 1)
    if ($oldShaExit -ne 0 -or -not $oldSha) {
        $result.Error = "could not resolve current HEAD"
        return [pscustomobject]$result
    }
    $result.OldSha = [string]$oldSha

    # A second recovery within the same timestamp must not fail just because a
    # backup name already exists.
    $baseName = "backup/pre-reset-$Timestamp"
    $backupBranch = $baseName
    $suffix = 2
    while ($true) {
        & git -C $RepoRoot show-ref --verify --quiet "refs/heads/$backupBranch"
        if ($LASTEXITCODE -ne 0) { break }
        $backupBranch = "$baseName-$suffix"
        $suffix++
    }

    & git -C $RepoRoot branch $backupBranch HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $result.Error = "could not create backup ref $backupBranch"
        return [pscustomobject]$result
    }
    $result.BackupBranch = $backupBranch

    $statusOut = @(& git -C $RepoRoot status --porcelain 2>$null)
    $result.HadLocalChanges = ($statusOut.Count -gt 0)
    if ($result.HadLocalChanges) {
        & git -C $RepoRoot stash push -u -m "pre-reset $Timestamp" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $result.Error = "local changes present but stash failed"
            return [pscustomobject]$result
        }

        $stashRefRaw = @(& git -C $RepoRoot rev-parse "stash@{0}" 2>$null)
        $stashRefExit = $LASTEXITCODE
        $stashRef = ($stashRefRaw | Select-Object -First 1)
        if ($stashRefExit -ne 0 -or -not $stashRef) {
            $result.Error = "stash completed but its recovery ref could not be resolved"
            return [pscustomobject]$result
        }
        $result.StashCreated = $true
        $result.StashRef = [string]$stashRef
    }

    $resetExit = Invoke-GitResetQuiet -RepoRoot $RepoRoot -Target $Upstream
    if ($resetExit -ne 0) {
        # The normal failure shape leaves HEAD untouched, but explicitly return
        # to the captured SHA so a partial reset cannot strand the checkout.
        [void](Invoke-GitResetQuiet -RepoRoot $RepoRoot -Target $oldSha)
        if ($result.StashCreated) {
            # Apply, do not pop: the immutable stash ref remains available even
            # if restoring the worktree reports a conflict.
            & git -C $RepoRoot stash apply --index $result.StashRef 2>&1 | Out-Null
        }
        $result.Error = "reset --hard $Upstream failed"
        return [pscustomobject]$result
    }

    $result.Success = $true
    return [pscustomobject]$result
}
