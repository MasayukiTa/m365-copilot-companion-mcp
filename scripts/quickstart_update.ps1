# quickstart_update.ps1 -- the update step for quickstart.bat.
#
# PURPOSE. quickstart.bat STEP 3 used to run `git pull --ff-only` directly, which
# fails forever on a REWRITTEN UPSTREAM (the project's main was once force-pushed to
# scrub bad commit metadata, so every clone taken before that is BEHIND and AHEAD at
# once and cannot fast-forward). A new PC that started from an old clone then kept
# running the OLD code and failed in scripts\bootstrap.py STEP 1/7. This wrapper makes
# quickstart.bat reuse the SAME recovery start_all.ps1 (Check-ForUpdates) uses: it
# dot-sources scripts\update_recovery.ps1, computes behind/ahead/canFastForward exactly
# as start_all.ps1 does, and runs Invoke-RewrittenUpstreamRecovery when the strategy is
# rewritten-upstream or diverged-unknown, else a plain `git pull --ff-only`.
#
# CONTRACT WITH quickstart.bat (exit codes):
#   0  an update was applied (fast-forward or recovery). The batch file may have been
#      rewritten underneath cmd, so quickstart.bat MUST stop and ask the user to re-run.
#   3  already up to date -- nothing was changed; quickstart.bat continues normally.
#   1  the update could NOT be applied (recovery failed, pull failed, no upstream, not a
#      work tree, bad git). quickstart.bat keeps running the code it has now and says so.
#
# The split of 0 (changed, stop) vs 3 (unchanged, continue) matters: the stop-after-update
# rule exists only because a SUCCESSFUL update replaces this very script on disk. An
# up-to-date checkout changed nothing, so continuing in the same window is safe.
#
# ASCII / ENGLISH ONLY: cmd corrupts non-ASCII, and this is invoked from a .bat.

[CmdletBinding()]
param(
    # Repository root. Defaults to the parent of this script's folder (scripts\..).
    [string]$RepoRoot = "",

    # Optional git proxy argument string exactly as quickstart.bat builds it, e.g.
    # "-c http.proxy=http://host:port". Passed through to fetch/pull so the proxy that
    # quickstart.bat detected reaches this script too. Empty means no proxy override.
    [string]$GitProxyArg = ""
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    return (Split-Path -Parent $here)
}

# Split a proxy argument string like '-c http.proxy=URL' into an argv array git can take.
# Only the exact two-token '-c <key>=<value>' shape quickstart.bat produces is honoured;
# anything else is ignored rather than spliced into a command line unquoted.
function Get-ProxyArgs {
    param([string]$Raw)
    if (-not $Raw) { return @() }
    $trimmed = $Raw.Trim()
    if ($trimmed -match '^-c\s+(http\.proxy=\S+)$') {
        return @("-c", $Matches[1])
    }
    return @()
}

try {
    $root = Resolve-RepoRoot -Explicit $RepoRoot
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    . (Join-Path $scriptDir "update_recovery.ps1")

    $proxyArgs = Get-ProxyArgs -Raw $GitProxyArg

    # Never let git prompt for credentials (would hang a double-clicked window).
    $env:GIT_TERMINAL_PROMPT = '0'

    # 1) Must be a git work tree.
    & git -C $root rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[update] not a git work tree -- nothing to do."
        exit 1
    }

    # 2) Refresh remote-tracking refs. quickstart.bat already fetched, but fetching again
    #    is cheap and makes this script correct even if invoked on its own. A failed fetch
    #    is not fatal here: the counts below simply read whatever tracking refs exist.
    & git -C $root @proxyArgs fetch --quiet 2>$null | Out-Null

    # 3) Behind count. Upstream unset -> fails -> cannot update.
    $behindRaw = & git -C $root rev-list --count "HEAD..@{u}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[update] no upstream to compare with -- skipping update."
        exit 1
    }
    $behind = 0
    if (-not [int]::TryParse(($behindRaw | Select-Object -First 1), [ref]$behind)) {
        Write-Host "[update] could not read the behind count -- skipping update."
        exit 1
    }
    if ($behind -le 0) {
        Write-Host "[update] already up to date."
        exit 3
    }

    # 4) Ahead count and fast-forwardability -- computed EXACTLY as start_all.ps1's
    #    Check-ForUpdates does, so the same clone is classified the same way by both paths.
    $aheadRaw = & git -C $root rev-list --count "@{u}..HEAD" 2>$null
    $aheadExit = $LASTEXITCODE
    $ahead = 0
    if ($aheadExit -ne 0 -or -not [int]::TryParse(($aheadRaw | Select-Object -First 1), [ref]$ahead)) {
        $ahead = 0
    }
    & git -C $root merge-base --is-ancestor HEAD "@{u}" 2>$null | Out-Null
    $canFF = ($LASTEXITCODE -eq 0)
    $strategy = Get-UpdateStrategy -Behind $behind -Ahead $ahead -CanFastForward $canFF
    Write-Host "[update] behind=$behind ahead=$ahead canFastForward=$canFF strategy=$strategy"

    if ($strategy -eq 'rewritten-upstream' -or $strategy -eq 'diverged-unknown') {
        # RECOVERY PATH: `pull --ff-only` would fail forever on this shape (by design -- it
        # must never silently merge/rebase over local work). Take the same guided reset
        # start_all.ps1 uses: HEAD is backed up to a backup/pre-reset-* branch and any local
        # changes are stashed before the checkout is reset to the upstream.
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $recovery = Invoke-RewrittenUpstreamRecovery -RepoRoot $root -Upstream "@{u}" -Timestamp $stamp
        if (-not $recovery.Success) {
            Write-Host "[update] recovery aborted: $($recovery.Error)"
            exit 1
        }
        Write-Host ("[update] recovery: reset to @{{u}} succeeded " +
                    "(backup=$($recovery.BackupBranch) stashed=$($recovery.StashCreated) " +
                    "stashRef=$($recovery.StashRef))")
        exit 0
    }

    # strategy -eq 'fast-forward' (the only remaining possibility once behind > 0).
    & git -C $root @proxyArgs pull --ff-only 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[update] fast-forward pull failed."
        exit 1
    }
    Write-Host "[update] fast-forward applied."
    exit 0
} catch {
    Write-Host "[update] unexpected error: $($_.Exception.Message)"
    exit 1
}
