# Recovery from an unfinished-merge / unmerged-index checkout

## Hypothesis (verified by reproduction)

The checkout was stuck in an unfinished merge left behind by an earlier
`git pull` against a since-rewritten (force-pushed) upstream. In that state the
index has unmerged paths and `MERGE_HEAD` is present.

### Reproduction

A disposable repository was driven into the same shape: a pull that starts a
conflicting merge leaves the index unmerged and `MERGE_HEAD` set. Observed state:

```
PULL_EXIT=1 MERGE_HEAD=True UNMERGED_COUNT=3
```

Against the shipped `Invoke-RewrittenUpstreamRecovery`, this produced:

```
Success=False
Error=local changes present but stash failed
```

Root cause: `git stash push -u` refuses to run while the index has unmerged
paths, so the function returned before it ever reset to the upstream.

## Fix

In `scripts/update_recovery.ps1`, after the backup branch is created (which
preserves the original HEAD), and before the stash step:

- Detect an in-progress operation via `MERGE_HEAD`, `REBASE_HEAD`, or
  `CHERRY_PICK_HEAD`, or a non-empty `git ls-files -u`.
- Abort it with `git merge --abort`, falling back to a hard reset to `HEAD`
  if the abort is not applicable.
- The backup branch and any stash are never touched, so local work is retained.

## Verification

Re-running the reproduction after the fix, from the identical unmerged +
`MERGE_HEAD` state:

```
Success=True
Error=
HEAD_after == Upstream
```

The in-progress merge is cleared, the worktree is clean, and the backup branch
still points at the pre-recovery HEAD.

## Tests

Two Pester 3.4.0 integration tests were added to
`scripts/update_recovery.Tests.ps1`:

1. Recovers a checkout stranded in an unfinished merge with an unmerged index:
   asserts the recovery succeeds, lands on the rewritten upstream, leaves a
   clean worktree, and preserves the backup branch.
2. Leaves a clean, non-conflicted checkout untouched except for the reset
   (guards against a spurious abort when no operation is in progress).

Full suite, with Pester pinned to 3.4.0:

```
Passed: 13  Failed: 0
```
