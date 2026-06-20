# SWE-bench Lite 300 Miss Analysis

Date: 2026-06-20

Scope: all **85 misses** from the strong-scaffold SWE-bench Lite 300 run.

Final score context: `215/300 = 71.7%` pass@1. The miss set is `82 unresolved + 2 error + 1 empty`.

Generated evidence:

- Full bundles: `.fleet/swe/_miss300/<instance>.md`
- Index: `.fleet/swe/_miss300/miss_index.jsonl`
- Summary table: `.fleet/swe/_miss300/summary.md`
- Builder: `python bench/swe_lite300_miss_bundle.py`

The bundles include issue text, gold patch, agent patch, official `report.json` FAIL_TO_PASS /
PASS_TO_PASS status, and official `test_output.txt` tails where available. The two `error` ids and
one `empty` id do not have per-instance official report/test-output files in the pulled harness
log tree.

## Category Counts

| category | count | share of misses | what it means |
|---|---:|---:|---|
| same_file_precision_miss | 41 | 48.2% | The agent found the right file but the exact semantics/output/edge handling were off. |
| underfit_patch | 14 | 16.5% | The patch was too small: one side of a paired path was fixed but another required path remained stale. |
| regression | 14 | 16.5% | The new case was partly addressed, but existing PASS_TO_PASS behavior broke. |
| wrong_file_or_layer | 13 | 15.3% | The patch touched an adjacent caller/layer instead of the shared definition the symptom flows through. |
| official_eval_error | 2 | 2.4% | Official evaluator completed the batch with these ids in `error`; counted as not resolved. |
| empty_or_capture_failure | 1 | 1.2% | Captured prediction was `model_patch: null`; counted as an empty patch miss. |

## Repo Concentration

| repo owner | misses |
|---|---:|
| sympy | 27 |
| django | 21 |
| pytest-dev | 8 |
| sphinx-doc | 7 |
| psf | 6 |
| matplotlib | 4 |
| astropy | 3 |
| scikit-learn | 3 |
| pallets | 2 |
| pylint-dev | 2 |
| mwaskom | 1 |
| pydata | 1 |

## Main Findings

### 1. The largest class is not search failure; it is precision failure.

In 41 cases, the agent edited a file that overlaps the gold patch and produced no PASS_TO_PASS
regression, but at least one FAIL_TO_PASS test still failed. Examples:

- `django__django-11019`: correct area (`django/forms/widgets.py`), but the media merge semantics
  were incomplete; 10 FAIL_TO_PASS tests remained.
- `sympy__sympy-12171`: correct printer file, but output did not match established Mathematica
  conventions and one existing power-printer test regressed.
- Multiple SymPy printer/parser cases: the agent fixed the obvious formatting hook but missed exact
  expected output, paired output paths, or existing convention.

Scaffold implication: a red->green reproducer must assert **exact output/errors**, not merely
"does not crash" or "some output exists".

### 2. Regression is a first-class miss class, not noise.

14 misses had PASS_TO_PASS regressions. The Requests cluster is especially loud:
`psf__requests-1963`, `2148`, `2317`, `2674`, and `863` each broke dozens of existing tests.

Pattern: broad changes to common APIs, exception wrapping, sessions/models, printers, and routing
can satisfy a new symptom while violating old behavior.

Scaffold implication: for common/public paths, the agent must identify a nearby old behavior and
keep it green before DONE. A "new case passes" check is insufficient.

### 3. Underfit patches often fix the producer but not the consumer.

14 misses were too small. Examples:

- `astropy__astropy-14182`: accepted `header_rows` for RST writing but missed the corresponding
  read/start-line behavior.
- `django__django-16820`: touched the right migration-operation file but left multiple related
  paths unhandled.

Scaffold implication: any changed shape/value must be traced through producer -> formatter /
normalizer -> consumer/re-emitter. One-hunk fixes are suspect when the value is later read,
printed, cloned, copied, or re-emitted.

### 4. Wrong-layer fixes are still common.

13 misses touched files disjoint from the gold patch. Examples:

- `django__django-15213`: patched SQL compiler output, but the durable fix belonged in the field
  formatting layer.
- `sympy__sympy-14024`: patched simplification rules and tests, but the real issue was numeric
  power semantics.
- Several matplotlib/pytest/sympy misses patched a caller or display point rather than the shared
  definition that all relevant paths use.

Scaffold implication: the agent needs an explicit "symptom path vs edited path" ledger before DONE.
If it cannot explain why the failing value flows through the edited function, it should keep tracing.

### 5. Problem statements can be bait.

Some issues include suggested snippets that are approximate rather than convention-correct.
`sympy__sympy-12171` is the clean example: the issue text suggested simple Mathematica printer
methods, but the established output convention required `Hold[D[...]]`, and changing float printing
regressed `test_Pow`.

Scaffold implication: issue snippets are clues, not commands. The agent must check local conventions
before copying a suggested fix.

## Strengthening Applied

The miss-85 lessons are now folded into `relay/quality_cards.py`, not kept as a SWE-only wall of
prompt text. `bench/swe_batch_setup.py` and general verified coding goals both call the same helper,
which selects short cards from the task text:

- symptom/edit/verification evidence line
- exact-output checks for output, parser, formatter, warning, and error tasks
- paired-path checks for read/write, parse/print, producer/consumer, clone/copy, and state tasks
- regression checks for public APIs and common semantic paths
- layer checks for dispatcher, normalizer, backend, compiler, renderer, and shared-definition tasks
- convention checks when issue snippets may be approximate

Controls:

- `AGENT_QUALITY_CARDS=0` disables the shared adaptive cards globally.
- `SWE_MISS85_DISCIPLINE=0` still disables the SWE-bench call site for A/B compatibility.

## Next A/B

Run a sealed slice with:

- ON: `SWE_STRONG_SELFTEST=1 SWE_MINIMALITY=1 SWE_FIX_RADIUS=1 SWE_MISS85_DISCIPLINE=1`
- OFF: same, but `SWE_MISS85_DISCIPLINE=0`

Do not use these 300 instances for a headline score. They are now training material for scaffold
development.
