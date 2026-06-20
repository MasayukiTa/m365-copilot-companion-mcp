# SWE-bench Lite 300 strong-scaffold scorecard

Final status for the decoupled 300-instance run completed on 2026-06-20.

This is **not the barefoot headline** from `bench/SCORECARD_clean_singleshot.md`.
It measures the current strong scaffold: solve locally with the general scaffold discipline
(`SWE_STRONG_SELFTEST`, `SWE_MINIMALITY`, `SWE_FIX_RADIUS`) and grade each produced patch once
with the official SWE-bench harness on kiyus. The hidden official tests were not fed back into
the solve loop.

## Result

**pass@1 = 215 / 300 = 71.7%**

Wilson 95% CI: **[66.3%, 76.5%]**

Source of truth:

- predictions: `.fleet/swe/preds_solve/*.json` (300 files, 299 non-empty patches)
- official batch reports: `.fleet/swe/_grade_batch/b0620191201.batchresult.json` and
  `.fleet/swe/_grade_batch/b0620220832.batchresult.json`
- recompute: `python bench/swe_lite300_scorecard.py`

Swebench completed all 300 verdicts. `error` and `empty` ids are counted as not resolved here,
because the official evaluator returned a completed report for them; they are model/output misses,
not eval-host gaps.

| bucket | count |
|---|---:|
| resolved | 215 |
| unresolved | 82 |
| error | 2 |
| empty | 1 |
| total | 300 |

Error ids: `pallets__flask-4992`, `sympy__sympy-20639`.

Empty id: `sympy__sympy-16503` (`model_patch: null` captured as an empty patch).

## Repo Breakdown

| repo | resolved | total | pass@1 |
|---|---:|---:|---:|
| astropy | 3 | 6 | 50.0% |
| django | 93 | 114 | 81.6% |
| matplotlib | 19 | 23 | 82.6% |
| mwaskom | 3 | 4 | 75.0% |
| pallets | 1 | 3 | 33.3% |
| psf | 0 | 6 | 0.0% |
| pydata | 4 | 5 | 80.0% |
| pylint-dev | 4 | 6 | 66.7% |
| pytest-dev | 9 | 17 | 52.9% |
| scikit-learn | 20 | 23 | 87.0% |
| sphinx-doc | 9 | 16 | 56.2% |
| sympy | 50 | 77 | 64.9% |

## Run Notes

- The solve phase completed at 2026-06-20 21:14:37 JST:
  `captured 300/300 (non-empty diffs: 299)`.
- Batch `b0620191201` covered 258 instances and completed on kiyus after the local 60-minute
  poll deadline; its actual official result was 184/258.
- Batch `b0620220832` covered the remaining 42 SymPy instances and returned 31/42.
- The grading script now defaults to a 120-minute batch ceiling and normalizes `model_patch: null`
  to an empty patch before writing predictions.

## Interpretation

The 300-instance CI is now tight enough to treat the strong scaffold as roughly low-70s pass@1 on
SWE-bench Lite under this protocol. The score should not be compared to loop-inclusive numbers
that iterate against official grading feedback, and it should not replace the barefoot scorecard:
it measures the scaffolded agent, not the raw issue-only agent.
