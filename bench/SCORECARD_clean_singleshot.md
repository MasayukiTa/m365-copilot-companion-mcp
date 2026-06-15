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
  can pass on one run and miss on another. So per-instance ON-vs-OFF flips can be variance, not
  signal. A clean research on/off comparison needs larger N or multiple runs per arm.
- N=12 is a first signal; CIs are wide.

## Research ON arm
In progress (`swe_singleshot.py --research`, same 12 instances). Compares research-delegation effect
vs the 4/12 OFF baseline. (Results: `.fleet/swe/_clean_ss_research_results.txt`.)

_See bench/BENCHMARK_ROADMAP.md for the full axis plan. 2026-06-15._
