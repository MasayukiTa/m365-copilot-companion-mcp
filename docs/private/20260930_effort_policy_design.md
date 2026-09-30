# Per-goal effort policy: design, phase 1 (shadow only)

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
`shadow`: rows are written, behaviour is identical. `on`: NOT implemented in phase 1; treated
exactly as `shadow` and logged once per process.

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

## Phases 2-5

2. Child initial effort at fan-out: set `metadata["effort"]` on the child envelopes from
   `initial_level`. That is in `relay/fanout.py`, which is being changed by the codex work;
   coordinate with it first, do not edit in parallel.
3. Live switching (`on`): apply `evaluate` decisions to a running worker's knobs at the turn
   boundary, keeping the per-goal caps. Needs the worker to accept knob changes mid-goal.
4. Bench A/B on non-burned problems: uniform min/auto/ultra vs policy. Success is pass@1 per
   token, not DONE (a self-report).
5. Model/agent switching, only if Copilot supports it: UNVERIFIED, not assumed.

## Tracking

This work is not yet listed in `docs/private/20260930_current_stabilization_tasks.md`. That
file is owned by codex and was deliberately not edited here.
