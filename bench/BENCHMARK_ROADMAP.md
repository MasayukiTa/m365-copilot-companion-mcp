# Coding-ability benchmark roadmap

Goal: a **trustworthy, contamination-aware** measure of the companion (Opus 4.8) agent's coding
ability across multiple axes. Honest protocol throughout: **single-shot pass@1** (no
grader-iteration), no test-specialization, fresh/sealed instances, **report CIs not point
estimates**, disclose every confounder.

Framing: the current SWE-bench Lite numbers are the **"barefoot" CI bounds** — single-shot, no
research delegation, no test-specialized scaffold. They are the floor; scaffold/research lifts go
on top and are measured separately so the gain is attributable, not baked into the headline.

---

## Phase 1 — SWE-bench Lite, single-shot pass@1 (IN PROGRESS)

- **Strong scaffold full Lite 300 (2026-06-20)**: decoupled solve locally, official one-shot
  grading on the eval host. This is NOT the barefoot headline; it measures the current general scaffold
  (`SWE_STRONG_SELFTEST`, `SWE_MINIMALITY`, `SWE_FIX_RADIUS`) without hidden official-test feedback
  during solve.
  - Final: **215/300 = 71.7%**, Wilson 95% CI **[66.3%, 76.5%]**.
  - Source: `bench/SCORECARD_swebench_lite300_strong.md`; recompute with
    `python bench/swe_lite300_scorecard.py`.
- **Barefoot (research OFF)**: `bench/swe_singleshot.py` — agent sees the issue only, NO acceptance
  check during solving (`run_relay checks=None`) → one patch → graded ONCE. No grader-iteration,
  no regression feedback. Fresh slice from `bench/swe_clean_setup.py` (lite_local − holdout − burned).
  - First signal n=12 = **4/12 ≈ 33%** (correcting 1-2 harness no-report eval-failures upward).
  - 95% CI at n=12 is wide (~[14%, 61%]). **Scale to n≈300** to tighten; predicted barefoot range
    **~30-80%** (true value lands inside as N grows).
- **Research ON** (`swe_singleshot.py --research`): same 12-instance slice, research delegation
  enabled. **First run (n=12, 2026-06-15): ≈4/12 ≈ the OFF 4/12 — no measurable effect.** Two
  findings: (1) the agent **did not actually delegate** any `RESEARCH:` query on these local-code
  bugs (it solved directly in ~1 turn), so research-ON was effectively a *second OFF run* — the
  per-instance ON/OFF flips (sympy, pytest) are the expected stochastic variance of two
  independent draws. (2) The arm's *raw* grade was a misleading **0/12**, caused entirely by a
  WSL eval-host wedge (false NOTs); healthy-WSL re-grade restored it to ~4/12. **Lesson: to
  measure the research axis you need tasks where the agent *chooses* to delegate (not local repo
  bugs), or a forced-research variant.** Paired ON/OFF on SWE local-bugs cannot separate the axis.
- **Reference point**: SICA ~53% on SWE-bench. The barefoot single-shot already approaches/overlaps
  it; with research/scaffold the upper bound is promising.
- **NOT comparable to leaderboard "95%"**: that was loop-inclusive (verify-retry against the grading
  tests) — disclosed separately as a confounded number, never the headline.

## Phase 2 — Contamination check: memory vs real ability

- **SWE-bench Live** (and/or multimodal/multi-repo variants): issues created AFTER the training
  cutoff → cannot be memorized. Run the SAME single-shot protocol.
- **Signal**: if SWE-Live ≈ SWE-Lite (barefoot), the ability is *real* (generalization). If
  SWE-Live ≪ SWE-Lite, the Lite number is inflated by **training contamination / memorization**.
- This is the decisive "記憶か実力か" test.

## Phase 3 — Language breadth (multilingual)

- A multilingual coding benchmark (multilingual SWE-bench / HumanEval-X / MBXP-style) to measure how
  far the ability extends beyond Python. Same single-shot protocol, per-language pass@1 + CI.

## Phase 4 — Algorithm axis (LiveCodeBench)

- **LiveCodeBench**: algorithmic/competitive problems, time-windowed (contamination-resistant). A
  different ability axis (algorithm design vs repo-bug-fix). Per-window pass@1 to keep it clean of
  contamination.

---

## Invariants (apply to every phase)
- **single-shot pass@1**, no grader-iteration, no test-name leakage, no test-specialized feedback.
- **fresh / sealed** instances; debugged instances are **burned** (excluded from any claim).
- **report CI + N**, not bare percentages; small-N numbers labelled as first signals.
- **disclose** all scaffold structure used (and research on/off, and any harness-meta usage).
- harness reliability matters: an eval that fails to produce a report (WSL/Docker wedge, timeout)
  must be **re-run**, not silently graded as a miss — else the pass@1 is undercounted. **Now
  enforced in code** (swe_check.py): the per-instance `report.json` is the eval-completion marker;
  a no-report triggers host recovery (`wsl --shutdown` + dockerd restart) and retry, and a
  persistent no-report returns **EVAL_ERROR (exit 2)** — a distinct signal that `swe_singleshot`
  excludes from the pass@1 denominator instead of charging to the agent. *Why this matters:* the
  first research-ON arm graded **0/12**, which on healthy-WSL re-grade was really **~4/12** — the
  0/12 was a pure eval-host artifact (genuine passes falsely graded NOT). Without this fix the
  measurement is not trustworthy.

_2026-06-15. Tracks: bench/SCORECARD_holdout60.md (loop-inclusive, confounded), the clean
single-shot results (_clean_ss_results.txt), [[perf-reporting-plan]] [[sica-paper]]._
