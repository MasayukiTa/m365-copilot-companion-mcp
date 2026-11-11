## 2026-11-10 - acknowledged goals dropped by a cold start are re-queued on runner start (PR #549)

- Result. The recurring loss is closed at the source. Measured 2026-11-10: each cold
  start of the stack (05:10 and 14:08 that day) silently dropped every goal that had
  been acknowledged -- 'landed': `.fleet/acks/<jid>.ack` and
  `.fleet/tasks/done/<jid>.landed.json` both present -- but had produced no outcome
  file. `status.json` after the restart read `workers=1 total=1` while the owners of
  five ShuttleScope goals and several orchestrator goals waited for hours with no
  worker and no outcome. The admission always survived on disk
  (`.fleet/intake_journal.jsonl` and `for_fleet/<jid>.txt` held what was submitted);
  nothing re-queued it. New `relay/orphan_readd.py` makes the runner start re-add each
  such orphan to the `for_fleet` queue exactly once, carrying a
  `resubmitted_after_restart` marker. Shipped via PR #549 on a fresh clone; all 11 CI
  checks pass (the hermetic `test` job green in 22m28s on the first run -- the known
  flaky health test did not fire, so no rerun was needed); `mergeStateStatus` CLEAN.

- Change. `relay/orphan_readd.py` computes the orphan set -- `orphan_jids(state_dir)`:
  an acked jid (ack file present) with NO outcome file
  (`tasks/done/<jid>.outcome.json`), NO live worker (no `status.json` `workers[].jid`
  carries it), and NO refused record (`tasks/done/<jid>.landed.json` with
  `status == "refused"`). `resubmit_orphans_on_start` parks each orphan back into
  `for_fleet/<jid>.txt` as a marked JSON object (`{"text": ..., "priority": false,
  "resubmitted_after_restart": true, "jid": ...}`), recovering the goal text from the
  still-parked for_fleet file or, if gone, from the intake journal's preview joined by
  jid. One log line is emitted per re-added jid and the count/jids are written to
  `.fleet/orphan_readd.json` ({count, jids, at}). The runner `main()` calls it on the
  same startup path as the existing resume/reconcile/retention housekeeping, before
  goals are finalised; `_snapshot` surfaces the count via a new additive
  `_orphan_readd_block` (reads the record file, off the hot path) under the
  `orphan_readd` key of `status.json`. `.github/workflows/ci.yml` adds the new test to
  the hermetic `test` job's explicit list.

- Behaviour. Terminal states are never touched: a jid with an outcome, a live worker,
  or a refused landing is excluded by the pure decision, so it is never re-added -- a
  refusal is a real decision by the command channel (it READ the add_goal and declined
  it), not a drop. Idempotent across starts by construction: the re-add parks the goal,
  but the *decision* is additionally gated on the goal not already sitting in for_fleet
  as a resubmission (the marker), so a second start that finds the same orphan still
  parked (not yet consumed) re-adds nothing and records count 0. Once the fleet consumes
  the parked goal and records any terminal outcome, the first gate already excludes it.
  Housekeeping never raises onto the run: every disk read is guarded and any failure
  degrades to "re-added nothing", logged, never propagated -- a start is never failed by
  this.

- Tests. `relay/test_an_acked_goal_dropped_by_a_restart_is_readded_once.py` builds a
  fake `.fleet` with three acked jids -- one with an outcome (jidDONE), one with a live
  worker (jidLIVE), one orphan (jidORPH) -- and asserts `orphan_jids` names only the
  orphan, that `resubmit_orphans_on_start` re-adds only the orphan (not DONE/LIVE),
  carrying the marker, logging it once, recording `count==1 jids==["jidORPH"]`, and
  parking it as a marked resubmission; then a SECOND start with nothing consumed re-adds
  nothing (`count==0`). Two further tests pin the refused-landing exclusion and the
  intake-journal text fallback. Added to the ci.yml hermetic list (so it is actually
  collected, not green-but-never-run).

- Discussion. The fix is at the source the goal said to target -- the runner startup
  path next to the existing resume and `fleet_reconcile` code
  (`scripts/supervisor.ps1 Invoke-FleetAutoResume`, `relay/fleet_reconcile.py`) -- not
  a sweep that would re-walk `acks/` on the hot path. The decision joins the same
  per-jid disk layout the rest of the fleet already uses, so no new source of truth is
  introduced. The for_fleet marker (`resubmitted_after_restart`) is purely additive:
  `read_for_fleet` returns `text` and ignores extra keys, so an ordinary parked goal
  stays plain and only a resubmission is distinguishable -- which is also how the
  idempotency gate recognises its own prior re-add. The status count is a cheap
  single-file read that changes once per start, kept off the snapshot hot path exactly
  like `_auto_resume_block`. One risk accepted: if an operator manually clears
  `for_fleet/<jid>.txt` AND the intake journal has no preview for that jid, the orphan
  cannot be recovered and is logged for an operator rather than silently re-queued with
  empty text -- the correct failure, since re-adding an empty goal would be worse than
  surfacing it.

- Open. None. The change shipped via PR #549 (branch
  `fix/orphaned-acks-readd-on-restart`), CI green (11/11), mergeable CLEAN, awaiting
  merge. The fresh clone under `%TEMP%\m365-companion\agents\orphanreadd` is removed
  after this entry is pushed.
