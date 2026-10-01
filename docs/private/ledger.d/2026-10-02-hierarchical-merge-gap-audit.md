## 2026-10-02 - Hierarchical merge gap audit (resume and failure propagation)

- Change: an audit of nested-family resume and failure propagation with adversarial synthetic
  families (fake workers, hand-written ledgers, the switch patched True). Two bugs found, both
  fixed with small additive edits:
  * A nested merge that finished but whose parent slot row was never written (process died
    between the two writes) was always sealed MISSING. `fleet_resume.recover_nested_merge_answer`
    now looks for the answer first in the merge worker's outcome file, its transcript (first user
    turn must hold the family goal AND the merge heading, so the splitting worker's own
    transcript, whose last turn is a split proposal, is never taken) and history.json; a split
    proposal is refused; MISSING only when nothing is recoverable. A recovered row carries
    `recovered` and `source`.
  * A retried depth-1 child (new task id) minted a second nested family for the same parent slot
    (the nested id hashes the splitting task id), orphaning the first.
    `_spawn_children` now refuses a split whose (parent_campaign_id, parent_subtask_index) is
    already filled by another nested family, in memory or on the ledger.
  * `_note_nested_result` wrote at most one `nested` row per slot, so a nested merge that was
    STUCK and then retried to DONE left the slot MISSING for good while the family was in memory.
    It now writes one MISSING marker and, when a retry finishes DONE, one DONE row that
    supersedes it (readers take the last `nested` row); a DONE row is never superseded or
    duplicated, and when the parent family has left memory the ledger is consulted.
- Decision (scenario 6): the root merge already issued with the gap is NOT re-issued when the
  nested slot is later answered. A second delivery happens only through the existing
  `merge_requeued` cap of 1 (a merge queued but never finished); a person decides after that.
- Scenarios (tests/test_hierarchical_merge_adversarial.py, in CI):
  1. crash between a grandchild finishing and its result line, depth 3: PASS (recovered from the
     outcome file, one re-queue only for the unrecoverable case, each merge once, bottom-up)
  2. nested merge finished, slot row not written: BUG FOUND AND FIXED (recovery before sealing)
  3. retry of a split depth-1 child: BUG FOUND AND FIXED (duplicate nested family suppressed)
  4. grandchild budget refusal mid-tree: PASS (direct answer in the slot, no split text)
  5. two siblings splitting in the same sweep: PASS (the second sees the first's header and is
     refused; header counts stay within the tree limit)
  6. nested merge STUCK then retried: BUG FOUND AND FIXED for the in-memory case (MISSING -> DONE
     once); the cross-process case passed already; root merge not re-issued (decision above)
  7. resume over a family whose root merge is done: PASS (nothing queued, ledger byte-identical)
- Behaviour: `HIERARCHICAL_MERGE_READY` stays False; flat families are unchanged.
- Open: when the budget refuses the retry's split (rather than the duplicate guard suppressing
  it) the retry runs the slice directly while the first nested family still runs; the slot takes
  the direct answer (a DONE record beats the waiting one) and the nested merge's row is unused.
