# SWE-bench Verified — Fresh-slice generalization + scaffold A/B

Date: 2026-06-21

## Design

- **Dataset**: SWE-bench Verified (500), restricted to instances **NOT in our burned Lite-300**
  (overlap 93) -> fresh pool 407. Representative seeded uniform sample **N=80**
  (`_verified80.txt`, seed 20260621). Repo mix: django 31, sympy 16, sphinx 9, matplotlib 7,
  astropy 5, sklearn 4, pydata 3, pytest 3, pylint 2.
- **Why fresh**: the quality_cards / MISS85 discipline was derived from Lite-300 misses, which are
  now training material. Scoring on those would be optimistic. These 80 are unseen.
- **Solve**: local Copilot fleet, `effort=auto` (refuter + research, RAM-gated side-pages),
  conc=3, max-turns=50, strong scaffold (STRONG_SELFTEST+MINIMALITY+FIX_RADIUS) in BOTH arms.
- **A/B variable**: `SWE_MISS85_DISCIPLINE` (the adaptive quality_cards on the SWE goal).
  - **ON**: `=1` (cards on). **OFF**: `=0` (cards off). Same 80 instances, same everything else.
- **Grade**: the eval host, swebench native `run_evaluation --max_workers 12 --dataset_name
  princeton-nlp/SWE-bench_Verified`, one process (no env-image build race).

## Results

| arm | resolved/graded | pass@1 | Wilson 95% CI | EVALERR | empty diffs |
|---|---|---|---|---|---|
| **ON** (MISS85 cards on) | 69/80 | **86.2%** | [77.0, 92.1] | 0 | 0 |
| **OFF** (cards off) | 64/80 | 80.0% | [70.0, 87.3] | 0 | 2 |

Both arms graded clean (EVALERR=0) on identical instances.

**Task B (fresh generalization)**: the strengthened scaffold scores **86.2%** pass@1 on unseen
Verified instances. Because these are NOT the burned Lite-300, this is a trustworthy generalization
number, not an overfit one. (Reference only, not a delta: burned Lite-300 = 71.7%. Verified is
human-validated as solvable so its absolute level runs higher than Lite regardless of scaffold.)

## A/B delta (task A — paired, same 80 instances)

Contingency (resolved?):

| | OFF resolved | OFF not |
|---|---:|---:|
| **ON resolved** | 62 | 7 |
| **ON not** | 2 | 9 |

- net delta = **+5 instances = +6.2 pp** in favour of the cards
- discordant: **cards helped 7, hurt 2** — McNemar exact 2-sided **p = 0.180**
- helped ids span django, pylint, sphinx (x2), sympy (x3) — diverse repos, i.e. the gain is not a
  single-repo artefact. The 2 regressions are both sympy.

**Interpretation (honest):** the quality_cards effect is **directionally positive and consistent**
(7:2 discordant, +6.2 pp, plus 2 fewer empty diffs), but at N=80 it is **underpowered** — p=0.180
does not clear significance. Read it as suggestive, not proven. A conclusive delta would need a
larger fresh slice (the remaining 327 fresh Verified instances are available in
`verified_fresh_spec.json`). The headline deliverable that IS solid is the fresh pass@1 = 86.2%.

## Repro

- slice: `_verified80.txt` (seed 20260621 over Verified minus Lite-300)
- ON solve: `SWE_MISS85_DISCIPLINE=1 SWE_SIDEPAGE_RESERVE=0 python bench/swe_solve_decoupled.py
  --spec verified_fresh_spec.json --targets-file _verified80.txt --preds-dir preds_verified_on
  --tag von --effort auto --chunk 20 --max-concurrent 3`
- OFF solve: same with `SWE_MISS85_DISCIPLINE=0 --preds-dir preds_verified_off --tag voff`
- grade (each): `python bench/swe_grade_swebench.py --preds-dir preds_verified_{on,off}
  --targets-file _verified80.txt --dataset-name princeton-nlp/SWE-bench_Verified --max-workers 12`
