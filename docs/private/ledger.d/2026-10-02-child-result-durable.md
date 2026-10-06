## 2026-10-02 - A finished child's answer is durable, recoverable, and a stalled merge is noticed

- Change: the `child_result` campaign-ledger line was written on a later sweep, so a coordinator
  killed just after a child settled left the done-map saying DONE with no answer on disk. Resume
  skipped that child and the merge queue saw n-1 records forever (a live fan-out resume test hit
  it twice). Three layers: (1) `RelayWorker._settle_done` calls a durable writer
  (`_note_one_finished` in `run_relay_fleet`) the moment the outcome is DONE; the sweep stays as
  an idempotent safety net and never writes a second line for the same slice. (2)
  `fleet_resume.resume_children_goals` finds children DONE in the done-map without a result line
  and recovers the text from `tasks/done/<jid>.outcome.json`, then the worker transcript, then
  `history.json`, writing a `child_result` line marked `recovered`; if nothing has it the child
  is re-queued once (`child_requeued` marker line, read by `fanout.campaigns_from_ledger`). (3)
  `_stall_check` in the merge-queue sweep: a family short of records whose missing children are
  all DONE in the done-map for `MERGE_STALL_SWEEPS` (3) sweeps writes the mechanism row
  `merge_stalled_missing_child_result` and runs the same recovery. Mechanisms
  `child_result_recovery` and `merge_stalled_missing_child_result` are registered.
- Behaviour: merge exactly-once is unchanged (`merged`/`merge_done`/`merge_requeued` untouched;
  the stall path never queues a merge itself, the normal path does once the records are whole).
  Old ledgers load unchanged.
- Tests: `tests/test_child_result_durable.py` (in CI) covers present, recoverable from outcome
  file / transcript (plain and gz) / history, order of sources, unrecoverable re-queued once,
  no duplicate lines, durable write at settle time, stall detector, no second merge.
- Open: a transcript match relies on the child's goal text being in the first user turn; the
  stall check needs the done-map to be current in-process (otherwise resume does the recovery).
