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

## Temporary files: one swept home

Everything temporary that this product or an agent creates goes under `%TEMP%\m365-companion\`
(`relay/temp_home.py`: `temp_home()`, `temp_dir(name)`). `relay/fleet_retention.py` (`sweep_temp_home`,
run by `apply()`) deletes entries there that have had no write for 24 hours (override with the
env var `MCP_TEMP_HOME_MAX_AGE_H`). Nothing else in `%TEMP%` is ever swept.

- Put venvs, clones, build output and test scratch under `%TEMP%\m365-companion\agents\<name>\`
  and delete them when the work is done; the sweep is the backstop, not the plan.
- An idle directory is judged by its newest file. A long-lived venv you still need must be
  touched (or used) at least once a day, or put somewhere else.
- Do not write new scratch directly in `%TEMP%`: it is not swept and was the cause of the disk
  filling up.
