# SWE-bench Lite — Sealed Dev-Holdout (n=60) Scorecard

**Model/scaffold:** companion (Opus 4.8) + local swebench harness (WSL2 Docker).
**Gate:** strict — `resolved == true` iff ALL `FAIL_TO_PASS` pass AND ALL `PASS_TO_PASS` maintained.
**Source of truth:** swebench's own `report.json` per instance under
`/root/swe/logs/run_evaluation/agent_<inst>/companion/<inst>/report.json`.
**Holdout list:** `.fleet/swe/holdout_dev.txt` (60 instances). Sealed — not used for harness debugging.

## Headline

| verdict | count | of 60 |
|---------|-------|-------|
| **RESOLVED** | **57** | **95.0%** |
| not_resolved | 3 | 5.0% |

- **57/60 = 95.0% strict-resolved.** 56 from official `report.json` + matplotlib-18869 from
  clean re-eval (it was the only NO_REPORT; never debugged on → a legitimate clean resolution).
- **Genuine model-miss count among failures: 1 (sphinx-7738).** The remaining 2 (requests-2148/2317)
  are environment-blocked + burned, excluded from the claim either way.

## The 4 non-resolved, categorized

| instance | verdict | nature | counts against model? |
|----------|---------|--------|----------------------|
| sphinx-doc__sphinx-7738 | not_resolved | **genuine model miss** — patch didn't gate the trailing-underscore escape on `config.strip_signature_backslash` (PR #7738); 30/31 tests pass | **YES** |
| psf__requests-2148 | not_resolved | environment-blocked: test suite needs httpbin (unreachable/503 from this host) **+ BURNED** (harness debugged on it) | excluded |
| psf__requests-2317 | not_resolved | environment-blocked: same httpbin dependency **+ BURNED** | excluded |
| matplotlib__matplotlib-18869 | ~~NO_REPORT~~ → **RESOLVED** | clean re-eval passed (was never debugged on → legitimate) | resolved |

**Genuine model-miss count among failures: 1 (sphinx-7738).** The requests pair are
environment failures (not model errors) and are additionally *burned* (the local-httpbin
harness work was debugged against them), so per the no-overfitting / burned rule they are
**excluded from any score claim** rather than counted as model resolutions.

## Scoring integrity notes (SICA-style fixed scoring)

- **Strict gate, no partial credit.** A single PASS_TO_PASS regression fails the instance.
- **Burned instances excluded.** Any instance used to debug the harness is not claimed as a
  model resolution. Burned set: psf__requests-2148, psf__requests-2317 (httpbin harness).
- **Harness fixes are domain-general only** (WSL memory cap, per-repo disk-aware admission,
  eval-timeout env knob, `-rA` PYTEST_ADDOPTS shim for new-file-only test_patch, local
  httpbin for any requests instance, deterministic classify_failure path). No instance-specific
  test or patch shortcuts.
- **Source = official report.json**, not in-loop self-reports, so the verdict table is
  independently reproducible from the artifacts.

## Re-eval confirmation (in progress)

`bench/swe_remeasure.sh` clean-re-evals the un-logged worktrees sequentially (disk-safe) to
(a) fill the matplotlib-18869 NO_REPORT gap and (b) independently re-confirm the resolved set.
Results: `.fleet/swe/_remeasure_results.txt`. Verdict table: `.fleet/swe/_verdict_table.txt`.

_Last updated from official artifacts: 2026-06-14._
