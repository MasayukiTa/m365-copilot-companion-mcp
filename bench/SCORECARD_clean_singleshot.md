# Clean single-shot SWE-bench Lite pass@1 — honest scorecard

The confound-free number. **Single-shot pass@1**: the agent sees the issue only, NO acceptance
check during solving (`run_relay checks=None`) → ONE patch → graded ONCE by the official swebench
eval. No grader-iteration, no regression feedback, no test-name leakage. Fresh instances
(`lite_local − holdout − burned`). Runner: `bench/swe_singleshot.py`.

## Result (research OFF — "barefoot")

**pass@1 = 4/12 = 33.3%** (n=12 fresh, 2026-06-15). Confirmed: the 5 instances whose first eval
produced no report (a transient WSL/Docker wedge) were re-graded and are all genuinely not-resolved
— the 4/12 is NOT undercounted.

| repo | pass@1 |
|------|--------|
| scikit-learn | 2/2 |
| sphinx | 1/2 |
| sympy | 1/3 |
| django | 0/3 |
| pytest | 0/2 |
| **total** | **4/12 = 33%** |

95% Wilson CI at n=12 ≈ **[14%, 61%]** — wide. This is a first signal; scale to n≈300 to tighten
(predicted barefoot range ~30-80%). Reference: SICA ~53% on SWE-bench — barefoot single-shot
already overlaps it.

## Contrast with the loop-inclusive number (the confound made explicit)

| protocol | value |
|----------|-------|
| loop-inclusive (holdout, verify-retry against the grading tests) | 57/60 = **95%** |
| **clean single-shot (fresh, no grader-iteration)** | 4/12 = **33%** |

The ~3× gap is the net effect of iterating against the grading tests — a confound, not ability.
**The single-shot number is the honest, leaderboard-comparable one.** 95% is only ever reported as
"loop-inclusive, harness-retry-included," never as the headline.

## Failure analysis (general pattern, not instance-specific)

All 8 misses produced an on-target patch (right file + right approach). Examples: sympy-12171 added
the exactly-right `_print_Derivative`/`_print_Float` to the Mathematica printer; sympy-11870 a
reasonable trigsimp fix. They failed because **the exact patch was a step off / incomplete** — a
single-shot precision limit, NOT poor exploration or premature DONE. → generalization-direction
scaffold lift: `SWE_STRONG_SELFTEST` (run the repo's OWN tests before DONE to catch the imprecision
in one shot), A/B'd separately.

## Caveats
- **Single-shot pass@1 is stochastic** (the agent's solve is non-deterministic): the same instance
  can pass on one run and miss on another. **Direct evidence here:** OFF and research-ON both score
  4/12 but resolve *different* instances (OFF: sphinx-10451 + a sympy; ON: sphinx-10325 +
  pytest-11143). Per-instance ON-vs-OFF flips are variance, not signal. A clean comparison of any
  axis needs larger N or multiple runs per arm.
- **The eval host is the dominant confound, not the model.** The first research-ON grade was 0/12
  purely because the WSL/Docker host wedged and produced no report for 4 genuinely-passing patches.
  Always suspect the eval host on extreme values (0/N, N/N) and re-grade before concluding. Now
  enforced in swe_check (report.json marker + retry + EVAL_ERROR exit code).
- N=12 is a first signal; CIs are wide (95% Wilson ≈ [14%, 61%]).

## Research ON arm — result: 4/12 = 33% (identical to OFF; no measurable effect)

**research-ON pass@1 = 4/12 = 33.3%**, the SAME count as OFF — but the resolved SET differs, which
is stochastic single-shot variance, NOT a research effect:

| repo | OFF resolves | research-ON resolves |
|------|--------------|----------------------|
| scikit-learn | 10297, 10949 | 10297, 10949 |
| sphinx | 10451 | **10325** (different instance) |
| sympy | one of 3 | none |
| pytest | none | **11143** |
| **total** | **4/12** | **4/12** |

Two facts make this a non-result for the research axis, and both were only visible by LOOKING at
the run-logs (not the aggregate):
1. **The agent never actually delegated research.** The `RESEARCH:` strings in the run-logs are the
   goal's *instruction text*, not a query the agent emitted — it solved each bug directly in ~1
   turn. So research-ON was effectively a **second OFF run**; the per-instance flips are the
   expected variance of two independent draws landing on the same count (4) with different members.
2. **The raw research-ON grade was a misleading 0/12** — a pure WSL eval-host artifact (false
   NOTs). Healthy-WSL re-grade restored it to 4/12. This is now prevented in code: swe_check uses
   the per-instance `report.json` as the eval-completion marker → no-report triggers host recovery
   + retry, and a persistent no-report returns EVAL_ERROR (exit 2), never a silent miss.

**Conclusion: on SWE local-code bugs, research ON vs OFF is indistinguishable because the agent
does not choose to delegate. Measuring the research axis needs tasks where delegation actually
fires (not local repo bugs), or a forced-research variant.** (The earlier "research hurts" reading
was wrong on both counts — eval artifact + research never fired.)

_See bench/BENCHMARK_ROADMAP.md for the full axis plan. 2026-06-15._
