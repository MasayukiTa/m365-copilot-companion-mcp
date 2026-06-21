# System map — the self-improving, best-of-N M365 agent

Date: 2026-06-21

A single map of what exists, how the pieces connect, and what is measurement-safe-done vs fleet-gated.
Design rationale lives in SELF_IMPROVEMENT_CONTROLLER.md, SELF_GROWTH_L4_DESIGN.md, AGENT_STRENGTHS.md,
and M365_HARDENING_AND_UX.md; this is the index.

## Layers

1. **Substrate (pre-existing)** — the M365 Copilot fleet: drives N Copilot tabs over CDP, effort modes
   (min/auto/max/ultra), decoupled solve (`bench/swe_solve_decoupled.py`) + grade on kiyus
   (`bench/swe_grade_swebench.py`). The "hands". Cheap parallelism (a seat, not per-token) is the moat.

2. **Self-improvement controller ("brain", `relay/selfimprove/`)** — measure → diagnose → propose →
   validate → keep/revert, with a frozen statistical judge. The differentiator.

3. **Strengths (`relay/`)** — best-of-N, per-task confidence/abstention, calibrated competence,
   solve-policy routing. What a single-shot CLI structurally cannot do.

4. **Hardening (`relay/`)** — turn the substrate's fragility into regression-tested robustness.

## Module inventory (all stdlib, unit-tested, committed)

### Controller core — `relay/selfimprove/`
| module | role |
|---|---|
| `guards.py` | the 5 enforcing guards: BurnedRegistry, overfit_lint, significance_gate (McNemar), classify_outcome (infra-vs-real), proc_alive/launch_detached/done_after_last_start (process discipline) |
| `frozen.py` | frozen-constitution checksum: the agent may edit the scaffold, never the JUDGE; `frozen_intact` aborts the loop if a frozen file changed |
| `sentinel.py` | cross-dataset reward-hacking tripwire (a gain that regresses on a fixed canary is not kept) |
| `archive.py` | genome ledger + MAP-Elites QD map + build-on-parent selection |
| `propose.py` | generative PROPOSE harness (injected LLM generator, overfit-linted + diversity-suppressed) |
| `l2.py` | one gated iteration: frozen-pre/post + gate + sentinel + spend ceiling + default-safe queue |
| `policy.py` | L3 campaign: dataset rotation, plateau stop, tripwires |
| `apply.py` | genome store + frozen-safe commit (allowlist + dry-run default) |
| `calibration.py` | MEASURED pass@1 per task-class (Wilson CI); EVALERR excluded; `recommend_effort` |
| `targeting.py` | pick the weakest class + assemble its misses → feed propose (domain-general only) |
| `dashboard.py` | `dashboard_state` aggregator + scorecard (drives the WPF view) |
| `status.py` | unified operator view (scorecard + competence + next target) |
| `diversify.py` | N diverse genomes for a best-of-N run (base-first, distinct, domain-general) |
| `l2_cron.py` | scheduler-friendly single-iteration entrypoint (gated; dry-run default) |

### Strengths — `relay/`
| module | role |
|---|---|
| `bestofn.py` | the SELECTOR: pick the best of N candidate patches (selftest ≫ refuter ≫ consensus ≫ minimality) |
| `confidence.py` | per-task confidence + abstain/escalate over a best-of-N selection (calibrated humility) |
| `bestofn_run.py` | glue: N captured predictions → one ship/abstain decision |
| `solve_policy.py` | the CAPSTONE router: competence → how-to-solve (best-of-N vs single-shot) → finalize/decide |

### Hardening — `relay/`
| module | role |
|---|---|
| `edge_auth.py` | classify Copilot tab auth-state from PAGE STATE not URL (fixes the F4 wrong-kill) |
| `soak.py` | chaos harness: F1–F7 scenarios; RealInjector deferred; "fixed" only when soak is green |

### UI — `ui/`
`SelfImproveDashboard.cs` (WPF) renders `dashboard_state` (scorecard / A/B / burned ledger / archive),
wired into `FleetCockpit.cs` via an account_tree button.

## The two live data flows

```
SELF-IMPROVEMENT (continuous, get-better-at-your-code):
  calibration ─weak class→ targeting ─misses→ propose ─card→ l2(frozen+gate+sentinel) ─keep→ apply ─commit→ dashboard
                                                                          ↑ archive / burned / frozen enforce honesty

BEST-OF-N (per-task, do-it-many-ways-keep-the-best):
  solve_policy.plan_solve ─best-of-N?→ diversify(N genomes) ─[FLEET: N parallel solves]→ captures
        └─single-shot?→ [FLEET: 1 solve] ─┘                                                    │
  finalize = bestofn_run.decide → bestofn selector + confidence → winner + abstain/escalate ←──┘
```

## Status

- **Measurement-safe, done + tested + committed**: every module above (pure/policy layers), the WPF
  view (csc build-verified), the design docs.
- **Fleet-gated (after the running N=200 A/B completes)** — these touch fleet internals that the live
  A/B re-spawns, so editing them now would split its ON/OFF arms:
  1. run the actual N parallel solves for best-of-N (the one `[FLEET]` box above) → first dashboard win.
  2. adopt `edge_auth`/`guards`/admission/Edge-governor/watchdog-v2 into the fleet (the hardening
     pillars), each gated by `soak` going green.
  3. `#30` genome→scaffold application (wire `quality_cards` to read `apply.active_genome`) + expose
     per-id resolved sets from `loop.validate` so the sentinel can fire.
  4. schedule `l2_cron` (the autonomy step).

## Honest line

All the *machinery* is built and tested; what is not yet *proven* is the end-to-end best-of-N lift and
sustained compounding self-improvement (both need the fleet, post-measurement). The value already
banked: a coherent, honest, frozen-judge-guarded system that a per-token single-session CLI cannot
easily become.
