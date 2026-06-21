# Deploy runbook — turning the built modules into a running system

Date: 2026-06-21

Everything in SYSTEM_MAP.md is built + tested (20/20 suites green). This is the ordered, post-
measurement sequence to make it RUN. Every step that touches fleet internals must wait until the
running N=200 A/B finishes (editing them now splits its ON/OFF arms). Gate every change on the
regression suite + (for fleet changes) soak.

## 0. Always, before adopting anything

```
python -m relay.selfimprove.run_all_tests      # 20/20 must be green
```

## 1. Snapshot the constitution (one-time, when the frozen set is settled)

The frozen judge has no baseline yet, so `l2_cron --dry-run` aborts with NO_BASELINE (correct). Once
the graders / guards.py / design docs are in their intended state:

```
python -m relay.selfimprove.frozen --snapshot   # writes relay/selfimprove/frozen_baseline.json
python -m relay.selfimprove.frozen --verify      # INTACT
```
Re-snapshot only after a *deliberate* constitution change. Commit the baseline so frozen_intact has a
shared reference.

## 2. Best-of-N end-to-end → the dashboard's first "win" (FLEET edit)

The one fleet change that unlocks Bet #1. For a task:
```
plan = solve_policy.plan_solve(instance_id, calibration_report())   # best-of-N(N genomes) | single-shot
# FLEET: run plan["genomes"] -- N parallel solves of the SAME instance (apply each genome's knobs/cards),
#        capture one prediction per attempt into a per-task dir.
records = bestofn_run.load_candidate_dir(task_dir)
decision = solve_policy.finalize(records)        # winner + confidence + abstain
```
Then PROVE it: run best-of-N vs N=1 on a FRESH Verified slice, feed both through the controller's
gate (significance_gate + sentinel). If best-of-N shows a real gated lift → the dashboard renders its
first win. Start small (N=4, ~20 instances) to de-risk the fleet path before scaling.

## 3. Fleet hardening adoption — each gated by soak going GREEN (FLEET edits)

Wire `soak.RealInjector` to the real actions (the docstrings ARE the spec), then for each pillar:
adopt → run `python -m relay.soak --live` (post-measurement only) → require the scenario GREEN.

1. **F4** adopt `edge_auth.classify_live` into `edge_recover`: auto-renav on `redirect`, surface only
   true `needs_signin`. (Soak F4 must pass.)
2. **F5/F6** adopt `guards.proc_alive` (CIM) + `guards.launch_detached` across `fleet_runner` /
   orchestrators, replacing any `tasklist /FI "PID eq"`. (Soak F5/F6.)
3. **F1** Edge memory governor (recycle below cap, suppress side-pages under pressure) + RAM-aware
   admission (auto-lower conc as free RAM drops). (Soak F1.)
4. **F2** disk admission: bounded `.fleet/swe/work`, LRU-evict reconstructable clones, fail-soft not
   abort. (Soak F2.)
5. **F7** watchdog v2: slow-turn vs true-wedge discrimination + escalation ladder. (Soak F7.)

## 4. Self-modification execution (#30) (FLEET-adjacent edits)

1. Make `relay/quality_cards.py` MERGE in `apply.active_genome()` (so a kept genome's knobs/cards take
   effect). Edit after the measurement (quality_cards is re-imported per chunk).
2. Expose per-id resolved sets from `loop.validate` so `l2.run_iteration` can run the sentinel
   (currently gate-only fallback).
3. Flip `apply.safe_commit(..., dry_run=False)` ON only behind the frozen + gate + sentinel checks.

## 5. Autonomy (L2 → L3)

```
python -m relay.selfimprove.l2_cron --print-cron   # the scheduler command
```
Register that with Task Scheduler. Start at **L1** (human reviews each keep/revert: l2 default queues,
auto_commit=False). Graduate to **L2** (auto_commit only on a gate-passing, frozen-intact, sentinel-
clean result) once L1 has shown clean judgments for several iterations. Then **L3** (`policy.run_campaign`
with rotation + plateau stop + tripwires) for multi-iteration unattended campaigns.

## 6. The closing loop

Once 1–5 are live, the system runs: status → targeting picks the weakest class → propose a
domain-general card → l2 gates it on a fresh slice (best-of-N where weak) → keep iff significant + no
sentinel regression → apply + commit → dashboard shows the trend. Every gain proven; the judge never
edited. That is the identity from AGENT_STRENGTHS.md, deployed.
