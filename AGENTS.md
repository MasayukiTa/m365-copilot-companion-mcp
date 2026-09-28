# Repository agent rules

## Cross-PC delivery rule

A feature branch is staging, not delivery. Any fix or operational change that must be available on another PC is **not complete** until it is merged into `main` and pushed to `origin/main`.

For cross-PC / production-relevant fixes:

1. Develop and test on a branch.
2. Push the branch and open a PR to `main` so CI, Windows build, CodeQL, secret scan, and applicable workflow checks run.
3. Do not merge while required checks are failing or while the working tree contains unrelated changes.
4. Merge to `main` only after the change is stable enough to be the repository's portable baseline.
5. Verify the merged commit is present on `origin/main`; a branch-only push does not make the change available to another PC.
6. After merge, inspect the `main` workflow runs. A green feature branch with a red `main` is not a finished delivery.

Do not postpone this integration step when the current work is specifically about stability, setup, portability, authentication, fleet scheduling, or anything needed on a second machine.