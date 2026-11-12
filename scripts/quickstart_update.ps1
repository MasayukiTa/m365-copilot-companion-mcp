# quickstart_update.ps1 -- the update step for quickstart.bat.
#
# WHY THIS EXISTS (gap measured 2026-11-10). quickstart.bat used to run only
# `git pull --ff-only`. On a clone whose upstream main was force-pushed (the
# project scrubbed bad commit metadata once), a fast-forward is impossible
# FOREVER, so quickstart printed "UPDATE FAILED -- this checkout is still on the
# older version" and then kept running the OLD code. A new PC that started from
# an old clone therefore ran stale code and failed in scripts/bootstrap.py
# STEP 1/7 (the venv step, tools.childproc import).
#
# start_all.ps1 (Check-ForUpdates) already recovers exactly this shape by
# dot-sourcing update_recovery.ps1 and calling Invoke-RewrittenUpstreamRecovery.
# This wrapper reuses THE SAME code so quickstart.bat recovers identically
# instead of failing. It computes behind / ahead / canFastForward exactly as
# Check-ForUpdates does, picks the strategy with Get-UpdateStrategy, and:
#   * fast-forward        -> git pull --ff-only  (today's behaviour, unchanged)
#   * rewritten-upstream  -> Invoke-RewrittenUpstreamRecovery (guided reset)
#   * diverged-unknown    -> Invoke-RewrittenUpstreamRecovery (guided reset)
#
# CONTRACT (read by quickstart.bat via the exit code -- it then prints the
# existing success or failure message; this script prints no user dialog):
#   exit 0  -> the checkout now matches upstream (updated, or already did).
#             quickstart.bat must STOP after this: a successful update can
#             rewrite quickstart.bat while cmd is mid-file, so it prints
#             "Updated. Please run quickstart.bat again." and exits.
#   exit 1  -> update did not complete; the checkout is unchanged and still on
#             the older version. quickstart.bat prints UPDATE FAILED and keeps
#             running the code it has now (best-effort, never blocks startup).
#
# The caller has already decided an update is wanted (it asked the user and
# counted BEHIND > 0); this script does not prompt.
#
# ASCII / ENGLISH ONLY.

[CmdletBinding()]
param(
    # Repo root. Defaults to the parent of this script's directory (scripts/..).
    [string]$RepoRoot = "",

    # Optional "-c http.proxy=..." style prefix, forwarded to the git calls that
    # touch the network (pull), mirroring quickstart.bat's QS_GIT_PROXY. Passed
    # as a single string so the batch file can hand its own value straight in.
    [string]$GitProxyArg = ""
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

. (Join-Path $PSScriptRoot "update_recovery.ps1")

# Split the optional proxy prefix (e.g. '-c http.proxy=http://p:8080') into
# argument tokens so it can be splatted into the git calls. Empty -> no tokens.
$proxyTokens = @()
if ($GitProxyArg -and $GitProxyArg.Trim()) {
    $proxyTokens = @($GitProxyArg.Trim() -split '\s+')
}

try {
    # Must be a git work tree. If not, there is nothing to update -- treat as a
    # non-update (let the caller carry on with the code it has).
    & git -C $RepoRoot rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { exit 1 }

    $env:GIT_TERMINAL_PROMPT = '0'

    # behind / ahead / canFastForward -- computed EXACTLY as start_all.ps1's
    # Check-ForUpdates does (quickstart.bat already fetched before calling this).
    $behindRaw = & git -C $RepoRoot rev-list --count "HEAD..@{u}" 2>$null
    if ($LASTEXITCODE -ne 0) { exit 1 }   # no upstream -> cannot update
    $behind = 0
    if (-not [int]::TryParse(($behindRaw | Select-Object -First 1), [ref]$behind)) { exit 1 }
    if ($behind -le 0) { exit 0 }         # already up to date

    $aheadRaw = & git -C $RepoRoot rev-list --count "@{u}..HEAD" 2>$null
    $aheadExit = $LASTEXITCODE
    $ahead = 0
    if ($aheadExit -ne 0 -or -not [int]::TryParse(($aheadRaw | Select-Object -First 1), [ref]$ahead)) {
        $ahead = 0
    }

    & git -C $RepoRoot merge-base --is-ancestor HEAD "@{u}" 2>$null | Out-Null
    $canFF = ($LASTEXITCODE -eq 0)

    $strategy = Get-UpdateStrategy -Behind $behind -Ahead $ahead -CanFastForward $canFF
    Write-Host "[quickstart-update] behind=$behind ahead=$ahead canFastForward=$canFF strategy=$strategy"

    if ($strategy -eq 'rewritten-upstream' -or $strategy -eq 'diverged-unknown') {
        # The force-push shape: pull --ff-only would fail forever here. Take the
        # same guided reset start_all.ps1 takes. It backs HEAD up to a
        # backup/pre-reset-* branch and stashes local work before resetting.
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $recovery = Invoke-RewrittenUpstreamRecovery -RepoRoot $RepoRoot -Upstream "@{u}" -Timestamp $stamp
        if (-not $recovery.Success) {
            Write-Host "[quickstart-update] Update failed: $($recovery.Error). Saved copy: $($recovery.SavedCopy); stash: $($recovery.StashRef); backup branch: $($recovery.BackupBranch)."
            exit 1
        }
        Write-Host "[quickstart-update] Updated to upstream. Local work saved: copy $($recovery.SavedCopy); stash $($recovery.StashRef); previous commits on branch $($recovery.BackupBranch)."
        exit 0
    }

    # strategy -eq 'fast-forward' -- today's existing behaviour, unchanged.
    & git -C $RepoRoot @proxyTokens pull --ff-only 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { exit 1 }
    exit 0
} catch {
    Write-Host "[quickstart-update] error: $($_.Exception.Message)"
    exit 1
}
