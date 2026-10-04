## 2026-10-05 - A merge lost twice becomes a visible failed merge

- Finding (A): a live depth-2 family showed one child answer on the ledger yet was merged. Not a
  count bug: the other children had ended terminal (STUCK) and were handed to the merge as
  named missing slices with the acceptance checks that forbid claiming completeness. The guard
  (`ready_to_aggregate` + `len(records) < n`) is unchanged; tests now pin both directions.
  One real defect found on the way: a rehydrated `child_result` row was always counted DONE,
  whatever outcome the row carried, so a MISSING row read as a success ("7/7 done"). The merge
  record now takes the row's own outcome.
- Change (B): `rehydrate_decision` kept its one-re-issue cap and then dropped the family
  silently. `fleet_resume.abandon_exhausted_merges` (called from `_campaigns_from_disk`) now
  writes one `merge_abandoned` ledger line, a `merge_abandoned` mechanism row, and for a nested
  family a MISSING `nested` row on the parent slot. `fanout.campaigns_from_ledger` reads the
  marker (old ledgers load unchanged); `family_view` reports merge_state `failed` and no longer
  makes the parent wait on a failed subgroup; resume does not re-queue an abandoned family.
- Tests: tests/test_a_lost_merge_is_visible.py (synthetic ledgers; registered in ci.yml).
- Open: the cap is not raised; a person decides about a re-run.
