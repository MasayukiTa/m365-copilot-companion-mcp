## 2026-10-02 - A slot whose child split again waits for the nested merge

- Change: a child that splits again ends FANOUT, which counts as finished, so its parent's merge
  counted the slot as done and read the child's split-proposal text as the slot's answer. Now:
  `_queue_ready_merges` treats a FANOUT child as WAITING until the nested family's merge has
  written a `nested` `child_result` row addressed to the parent campaign and slot (the nested
  header's `parent_campaign_id` / `parent_subtask_index`, now also written to the header and kept
  by `campaigns_from_ledger`). The row is written by `_note_nested_result` when the nested
  aggregator ends DONE (synchronously from `_note_one_finished`) and by
  `_note_nested_merge_failed` when it ends without DONE (STUCK etc.). `fanout.nested_result_row`
  builds the row: DONE only for a merge that finished DONE with every slice and real text; else
  the explicit `MISSING` outcome (a split proposal is refused as a slot answer), so the parent's
  `missing_slices` / `merge_acceptance_checks` name the slot instead of counting an empty success.
  The `merged` line of a nested family carries `missing` so the marking survives a restart.
  Resume: `fleet_resume.campaigns_of_run` follows the parent-campaign chain, a child that ended
  FANOUT with its nested family on the ledger is not re-queued, and a nested merge that finished
  without its slot row gets an explicit MISSING row once (`seal_finished_nested_slots`).
- Behaviour: flat families are byte-identical (golden digest of merge goal and ledger from main).
  `HIERARCHICAL_MERGE_READY` stays False; the new behaviour is reachable only with it patched
  True, and a test fails if production code sets it True. Flipping it is a later change after
  live verification.
- Tests: `tests/test_hierarchical_merge.py` (in CI): 2 and 3 level families, stuck grandchild,
  stuck nested merge, waiting slot, resume mid-tree, budget refusal, flat golden, switch guard.
- Open: live verification with real splitting workers; a MISSING slot written by the seal keeps no
  answer text (it is not recoverable from the ledger).
