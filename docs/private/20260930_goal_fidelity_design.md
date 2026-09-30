# Goal fidelity: continuation prompts must keep the whole goal (2026-09-30)

## The defect

Three trip-planning runs since 2026-09-28 showed workers drifting away from their goal after a few turns.
Diagnosed from transcripts:

1. `RelayWorker._task_anchor` (relay/relay_fleet.py) prefixed every continuation prompt with only the first
   160 characters of the goal's first line. In a goal of about 550 characters the one hard constraint (a
   start time and place, a fixed end time and station) sat after character 200. From turn 3 on the worker no
   longer saw it, answered a different question, and at turn 7 told the reviewer that the concrete itinerary
   "does not exist in the user's original text". The refuter saw the full goal, so the claim was a false denial.
   The wording was also code-oriented ("fixing", "retry file reads and commands") for a non-coding goal, and a
   fan-out child lost its scope block.
2. A recovery message (the unlock text) ends at the goal heading. Delivered as a steer or as a follow-up, its
   goal section was EMPTY, so it replaced the whole task for several turns. As a follow-up it became the new
   worker's goal, so a recycle prompt's goal was the unlock text. One worker declared DONE on such a turn.
3. `theme_from_goal` split at the first slash, so goals starting with a date collapsed into themes "10"/"2026".

## The fix

* Anchor design: restate the WHOLE goal, capped at `ANCHOR_GOAL_CAP` = 6000 characters. Past the cap the head
  (two thirds) and the tail (one third) are kept with an explicit `...(truncated N chars)...` marker; a fan-out
  child's scope block lives at the end and so survives. No hash block, no summarising.
* Wording follows the worker's own signal: with a verification card (`self.checks`) the working-tree wording
  is kept; without one the anchor is neutral ("original text, satisfy every condition"). No wording heuristic.
* Every continuation type is anchored: retry, continue, fix, split, escalating continue, refute-fix, verify-fix,
  research results, cap notices, steer follow-up.
* `fill_recovery_goal` puts the goal back into a recovery payload whose goal section is empty (steer path and
  `fleet_runner._follow_up`). `effective_goal` strips a recovery wrapper from a worker goal.
* `_recycle_job` / `_replay_job` raise `EmptyGoalError` instead of building an empty-goal prompt; the callers
  turn it into a logged STUCK / ERROR reason. The unlock text can never be the `Goal:` of a recycle prompt.
* `theme_from_goal` does not split at a slash between digits.

Tests: relay/test_continuation_keeps_the_goal.py (28), relay/test_theme_from_goal_dates.py (4). Mutation
check on a copy: 11 mutants (head-only anchor, always-code wording, no goal fill, recycle/replay empty goal
allowed, raw goal seeded, verify job unanchored, replay uses composed goal, follow-up unfilled, date split
restored, no cap) all killed.

## Metrics (scripts/goal_fidelity_report.py, tests: scripts/test_goal_fidelity_report.py, 13)

* Constraint retention: hard-constraint tokens from the goal (quotes, clock times, dates, words after cue words
  such as "must"/"only"), or explicit `--must`. Drift score = share of assistant turns after turn 1 that contain
  none of the tokens although an earlier turn did.
* False denials: assistant sentences claiming the original text lacks something, checked by string containment
  against the goal (false = the item IS in the goal; true = it is not; unresolved = no checkable item).

## Baseline (before the fix; transcripts under .fleet/transcripts, read-only)

Heuristic tokens:

| run | transcripts | assistant turns | later turns | drifted | drift score | false / true / unresolved denials |
|---|---|---|---|---|---|---|
| r6abc7f6c_a0 | 20 | 69 | 49 | 4 | 0.082 | 0 / 0 / 0 |
| r6abcd114_a0 | 5 | 8 | 3 | 1 | 0.333 | 0 / 0 / 0 |
| r6abcdb87_a0 | 1 | 6 | 5 | 0 | 0.000 | 1 / 0 / 0 |
| total | 26 | 83 | 57 | 5 | 0.088 | 1 / 0 / 0 |

With `--must 14:00 --must <start place> --must <end station>`: r6abc7f6c_a0 drift 0.163 (8/49), r6abcd114_a0 0.0,
r6abcdb87_a0 0.0; total 0.140 (8/57); false denials unchanged (1). Small numbers: most transcripts have 1-3 turns,
and the drift heuristic counts any reply without the tokens, so read these as a floor, not a rate.

## Planned before/after experiment

Same vague, constraint-late goal (about 550 characters, hard constraint after character 200), N runs on the
pre-fix tree ("before", live tree) and N on this branch ("after"), same worker count, same model settings.
Compare drift score, false-denial count, and turns to DONE with `goal_fidelity_report.py --must` using the exact
constraint tokens. The fix is credited only if drift and false denials fall in the after batch; the goal text
and tokens are chosen before either batch runs.

## Open risks

* The nudge constants (RETRY_JOB, FIX_JOB) still mention files and commands; only the anchor is neutral.
* The anchor is up to 6000 characters longer per continuation turn; token cost is not measured.
* A human steer still replaces the turn with the steer text alone (unchanged).
