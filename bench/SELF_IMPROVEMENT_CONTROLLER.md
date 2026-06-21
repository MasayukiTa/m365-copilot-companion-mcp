# Autonomous Self-Improvement Controller — design

Date: 2026-06-21

## Goal

Turn the *human-supervised* self-improvement loop demonstrated on 2026-06-21 (analyze misses →
derive domain-general lessons → edit scaffold → fresh paired A/B → keep/revert) into a **closed loop
the agent runs by itself**, while preserving the *judgment* that a human currently supplies.

The hands (solve / grade / analyze / self-edit / delegate) already exist. What is missing is the
**brain**: the controller that decides what to do, plus the **guardrails** that keep the loop honest.
This doc specifies both.

## What exists today (substrate — reuse, do not rebuild)

| capability | module |
|---|---|
| solve a task set (Copilot fleet, effort=auto) | `relay/fleet_runner`, `bench/swe_solve_decoupled.py` |
| grade on a clean host (swebench, any dataset) | `bench/swe_grade_swebench.py` + `kiyus_batch_grade.py` |
| miss analysis (parallel sub-agents) | Workflow tool / `bench/*miss*` |
| self-editable scaffold | `relay/quality_cards.py`, `relay/coding_discipline.py`, `bench/swe_batch_setup.py` |
| A/B gate (env toggle) | `SWE_MISS85_DISCIPLINE`, `AGENT_QUALITY_CARDS` |
| job routing / sub-delegation | `relay/task_router.py`, fleet add_goal |
| scorecard ledger (SICA-style) | adopted per `reference_sica_paper` |

## The loop

```
            ┌──────────── held-out rotation (fresh slices) ───────────┐
            v                                                          │
  (1) MEASURE ─► (2) DIAGNOSE ─► (3) PROPOSE ─► (4) VALIDATE ─► (5) KEEP/REVERT ─► archive
   baseline       failure         scaffold        fresh paired     significance
   pass@1 +       classes         candidates      A/B (ON/OFF)     gate + safety
   verdicts       (general only)  (lean, deduped) on NEW slice
```

1. **MEASURE** — run a fresh held-out slice through solve+grade; record pass@1 + per-instance
   verdicts. (decoupled solve + `--dataset-name` grade already do this.)
2. **DIAGNOSE** — cluster the misses into **domain-general** failure classes (the Workflow
   miss-analysis). Instances touched here become **burned**.
3. **PROPOSE** — generate candidate scaffold edits (new/changed adaptive cards or discipline).
   Must be lean (de-dup vs existing) and domain-general.
4. **VALIDATE** — fresh, **paired** A/B (ON vs OFF) on a slice that is **not** burned. McNemar.
5. **KEEP/REVERT** — keep iff the change clears the **significance gate** AND is positive; else
   revert. Either way, record to the archive so it is not re-proposed (anti ideation-fatigue).

## Guardrails — the judgment, encoded (this is the actual work)

Each is a reusable primitive (`relay/selfimprove/guards.py`), unit-tested:

1. **Burned-instance registry** — any instance used for diagnosis or A/B is appended to a ledger and
   **excluded from future score claims and future A/B slices**. Prevents the optimism that makes
   "burned Lite-300" unusable as a headline. (cf. `feedback_no_benchmark_overfitting`.)
2. **Overfit linter** — reject a proposed scaffold edit if it names a specific repo, instance id,
   file path, or test name. Scaffold text must be domain-general or it does not ship.
3. **Significance gate** — keep a change only if McNemar exact p < α (default 0.05) **and** net
   effect ≥ min_pp **and** paired N ≥ min_n. A +6.2 pp / p=0.18 result (the 2026-06-21 N=80 case)
   is recorded as *suggestive*, **not kept as a win** — it triggers "enlarge N", not "commit".
4. **Infra-vs-real classifier** — bucket each non-resolved instance: `infra` (EVALERR, consent/UI
   card stall, process death, disk-floor abort) vs `real` (genuine wrong/under/regressed patch).
   **Only `real` feeds DIAGNOSE.** Infra never becomes a scaffold "lesson". (cf.
   `project_swe_eval_host_confound`: research-OFF 0/12 was an artefact, not a real miss.)
5. **Process-discipline lib** — the operational lessons from this session, as helpers so the loop
   never trips on them:
   - liveness via **CIM `Win32_Process` CommandLine match**, never `tasklist /FI "PID eq"` (venv
     `python.exe` is a shim → real pid differs → false "died").
   - durable background launch via **`Start-Process -WindowStyle Hidden`** (Git Bash `nohup &` and
     harness `run_in_background`+`exec` both get reaped).
   - single-instance lock + reused-log de-stale (detect done only after the *last* run-start line).
   - disk floor + chunked stage/solve/release.

## Safety

- Every scaffold change ships **behind an env gate** so it is always A/B-able and revertible.
- The loop **commits only kept changes**, with the A/B evidence in the message; reverted candidates
  are archived, not committed.
- Auto-commit is gated by the significance gate **and** a clean infra classification (no commit if
  the run was infra-contaminated).

## Autonomy levels (ship incrementally)

- **L1 semi-auto** — one command runs one full loop iteration; a human reviews the keep/revert
  decision and the diff before commit. (Closest to what happened on 2026-06-21.)
- **L2 scheduled** — cron runs iterations; auto-commits only changes that pass all gates; everything
  else is queued for human review.
- **L3 autonomous** — L2 + automatic held-out rotation and dataset expansion when the fresh pool is
  near exhausted. Hard stop conditions (no significant gain for K iterations → pause + report).

## Anti-plateau (SICA lesson)

Naive iteration plateaus via overfitting + ideation fatigue. Mitigations: the burned registry forces
genuinely fresh evaluation; the archive prevents re-proposing dead ideas; a diversity requirement on
PROPOSE; and a stop-and-report when K consecutive iterations fail the significance gate.

## First milestone

L1 over the existing fleet + kiyus grade, with guards #1–#5 implemented and the 2026-06-21 Verified
fresh A/B as the regression fixture for the significance gate (it must classify +6.2pp/p=0.18 as
"not yet a win, enlarge N" — exactly the call made by hand).
