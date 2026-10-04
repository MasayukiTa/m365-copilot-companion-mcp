# Per-goal effort policy: design (phase 1 shadow, phase 2 initial assignment)

Date: 2026-09-30. Status: phase 1 implemented, default OFF, changes no behaviour.

## Problem

`--effort` is resolved once per run into four knobs (refuter, max_refute, max_research,
review_lenses). Every worker gets the same four. `relay/effort.py` already lets a goal name its
own level, but nothing ever writes `goal["effort"]`, so in production every worker's
`config_source` is `run`. A policy would decide, per goal, where to start and when to move.

Repository rule that shapes everything below: difficulty is NEVER inferred from a goal's
wording. Effort comes from something that knew (explicit goal effort, the parent goal, a
measured calibration) or from in-run evidence the worker itself produced.

## What phase 1 ships

* `relay/effort_policy.py`, pure (I/O only through injected callables).
  * Ladder `min < max < auto < ultra`, derived from `effort.LEVELS`; a test asserts the knobs are
    monotone non-decreasing along it.
  * `initial_level(goal, run_level, parent_level=, is_merge_turn=, calibration_level=)`.
    Precedence: explicit goal effort > parent-derived (fan-out child = one step below the parent,
    floor `min`; the merge/verify turn of a parent keeps the parent's level) > calibration >
    run level. Returns `(level, source)`.
  * `Signals` + `evaluate(state, signals, cfg)` -> `Decision(stay|up|down, target, reason)`.
    Up one step on: REFUTED with retries left, STUCK/REFUSED streak >= 2, no progress >= 2,
    verify failed, low confidence twice. Down one step on: 3 consecutive first-pass UPHELD, or
    budget pressure (which outranks every escalation rule). Guards: per-goal switch cap (3),
    hysteresis (no reversal within 2 turns of the last switch), floor/ceiling, and a goal's
    explicit effort is its floor. All thresholds are in `PolicyConfig`, overridable as
    `MCP_EFFORT_POLICY_<FIELD>`.
  * A switch consumes the evidence that caused it (otherwise one REFUTED would justify an
    escalation on every later turn).
* Hook in `relay/relay_fleet.py` (three lines): `shadow_assign` when a worker is created and
  `shadow_tick` in the `finally` of `RelayWorker._decide`. Both never raise and return at once
  when the mode is off.
* `scripts/effort_policy_replay.py`: read-only replay over `mechanisms.jsonl`.

## Modes

`MCP_EFFORT_POLICY=off|shadow|on`, default `off`. `off`: no code path runs, no row is written.
`shadow`: rows are written, behaviour is identical. `on`: initial assignment is active
(fan-out children, merge, sibling rule; see phase 2); live switching is still shadow. Logged once
per process.

## Reading the shadow telemetry

Mechanism `effort_policy` in `.fleet/mechanisms.jsonl` (registered in
`mechanism_telemetry.MECHANISMS`). Always `executed=false`, `changed_decision=false`.

* Initial row (`extra.event == "initial"`, turn 0): `after` = the level the policy would assign,
  `config_source` = why (`goal|parent|policy|run`), `extra.run_level` = what the worker really got.
* Per-turn row (`extra.event == "turn"`): `extra.level` current virtual level, `extra.decision`
  `stay|up|down`, `extra.reason` the rule (or the guard that held it: `switch cap`, `hysteresis`,
  `ceiling`, `floor`), `extra.signals` the exact evidence. `triggered` is true when it would
  have switched.

Limits to keep in mind: the low-confidence signal has no live source yet (always 0); the
first-pass-UPHELD streak is per worker, and an UPHELD ends a goal, so that rule cannot fire in
phase 1 (it needs a run-level streak); stuck/refused is edge-detected from `status`/`outcome`
and `fresh_replay_count`, so it mostly fires on the terminal turn.

## Phase 2: `on` = initial assignment (shipped, default still off)

`MCP_EFFORT_POLICY=on` now does exactly two things and nothing else. Live switching
(`evaluate`) stays record-only in every mode; the per-turn rows still say `mode: shadow`.

* **Fan-out children start one step below the parent** (floor `min`). `fanout.child_goals(...,
  parent_level=, run_id=)` calls `effort_policy.assign_children`, which writes
  `metadata = {effort, effort_source: "parent", parent_effort}` on each child. The worker path is
  unchanged: `effort.resolve` already honours `metadata["effort"]`. The parent's level comes from
  the knobs the parent worker holds (`effort_policy.worker_level`, via `level_of_knobs`); a
  hand-made knob set is `custom` and yields no assignment. A child that already names an effort
  keeps it (goal > parent).
* **The merge turn keeps the parent's level.** `aggregation_goal(..., parent_level=)` ->
  `merge_effort`. The level rides in `campaigns[cid]["parent_level"]` for the run; a family
  adopted from disk (resumed run) merges at the run's level, as before.
* **Sibling de-escalation.** `effort_policy.EVIDENCE` (a `CampaignEvidence`, bounded to 256
  campaigns, keyed by the existing campaign id) counts consecutive first-pass UPHELD subtasks.
  `observe_child` runs from `RelayWorker._decide`'s `finally`. UPHELD on the first refuter pass
  extends the streak; any REFUTED, any refutation before an UPHELD, or any non-DONE terminal
  resets it; DONE with no refuter verdict is no evidence. When the streak reaches
  `sibling_upheld_streak` (default 2, `MCP_EFFORT_POLICY_SIBLING_UPHELD_STREAK`), a sibling that
  is created LATER (`_worker_for` -> `sibling_adjust`) starts one more step lower, floor `min`,
  and only if its effort was policy-derived (`effort_source == "parent"`): an explicit effort is
  never lowered. The goal is copied, not mutated.
* **off / shadow leave children byte-identical** (test compares dicts and the merge goal). In
  shadow the rows say "would assign" / "would lower" and nothing is set.

Also fixed on the way: `level_of_knobs` compared `review_lenses` None against a worker's `[]`,
so every real worker read as `custom`.

### Reading the phase-2 rows (mechanism `effort_policy`)

* `extra.event == "child"`: one per child. `extra.verb` `assigned` (on, `executed=true`) or
  `would assign` (shadow); `before` = parent level, `after` = child level, `extra.campaign_id`,
  `extra.subtask_index`.
* `extra.event == "merge"` (on only): the merge kept the parent level.
* `extra.event == "sibling"`: `extra.verb` `lowered` / `would lower`, `extra.streak` = the streak
  that justified it, `extra.campaign_id`.
* `extra.event == "initial"` rows for children now show `config_source` `parent` / `sibling`
  (not `goal`), so the A/B can separate derived from asked-for efforts.

Known limits: evidence is in-process (a resumed run starts at streak 0); a subtask that ends
outside `_decide` (some error/cancel paths) is not observed; the shadow-mode sibling row has no
`before` level because shadow children carry none.

## Phases 3-5 (what remains)

3. Live switching: apply `evaluate` decisions to a running worker's knobs at the turn boundary,
   keeping the per-goal caps. Needs the worker to accept knob changes mid-goal.
4. Bench A/B on non-burned problems: uniform min/auto/ultra vs policy. Success is pass@1 per
   token, not DONE (a self-report). The phase-2 sibling threshold (2) is a guess until then.
5. Model/agent switching, only if Copilot supports it: UNVERIFIED, not assumed.

## Tracking

This work is not yet listed in `docs/private/20260930_current_stabilization_tasks.md`. That
file is owned by codex and was deliberately not edited here.
