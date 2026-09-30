# Effort-policy benchmark: design and capacity estimate (2026-09-30)

Research and design only. Nothing was run, submitted or changed. Evidence tags: **[M]** measured from an existing artifact (named), **[E]** estimate (arithmetic shown), **[U]** unknown.

## 1. Question, arms, N

**Primary question:** on fresh SWE-bench Verified problems, does the uniform effort level change (a) pass rate, (b) patch size in changed lines relative to the gold patch, (c) cost (turns, wall, tab-seconds)?
Arms: uniform `min`, `max`, `auto`, `ultra` (policy arm later, on held-back problems). Same problems in every arm, same `--max-turns 50`, same `--max-transient 8`, same scaffold env (`SWE_STRONG_SELFTEST/MINIMALITY/FIX_RADIUS`, which `bench/swe_solve_decoupled.py` sets for all arms), fan-out pinned identically (section 5), **2 repeats per arm**.

**What the prior evidence supports [M]:** n=6, one run per arm: 5/6 in all three arms, same 5 solved, ultra 14 turns vs 11 (project_effort_ab_20260830). Same-effort resampling gave 6/6 different patches and moved pass/fail; the effect of effort was smaller than sampling noise. The "44-47 line diffs vs 2-7 line gold" line (relay/fleet_runner.py:3455) has no run behind it [M: run_effort_ab.ps1 header says so].

**Power (paired, exact McNemar, alpha 0.05 two-sided, 80% power, true difference 10 points, baseline about 0.8):** required N = ((1.96*sqrt(psi) + 0.84*sqrt(psi - 0.10^2)) / 0.10)^2, where psi is the discordant-pair rate (pairs where exactly one arm passes).
psi 0.11 (the only measured value: 9/80 in the cards A/B, one run per arm [M]) gives 84; psi 0.15 gives 116; **psi 0.20 gives 155**; psi 0.25 gives 194; psi 0.30 gives 234 [E]. An unpaired design would need 197 per arm, so pairing is the saving. Monte-Carlo check of the exact test: N=150, psi 0.20 gives 75% power; N=200, psi 0.25 gives 79% [E].
**Choice: N = 156 problems** (psi 0.20 is a deliberately pessimistic-for-repeats guess, since repeat noise raises psi [U]). Power is about 0.7 if two primary contrasts are corrected to alpha 0.025 [E]. Two pre-registered contrasts only: min vs ultra (full range) and auto vs ultra (the over-engineering claim); the other four pairs are reported unadjusted as descriptive. Two repeats per arm lower per-problem noise, so realised power should be at or above these numbers, by an amount not known [U]. Analysis: per-problem score = mean of the 2 repeats; paired sign/Wilcoxon on scores, McNemar on run-level pass with problems as clusters (bootstrap over problems).
Runs: 156 x 4 x 2 = **1,248**.

**Stratification:** `.fleet/swe/verified_fresh_spec.json` (in the main checkout, untracked; it is all 500 Verified rows, not 327) has `repo`, `difficulty` (`<15 min fix` 194, `15 min - 1 hour` 261, `1-4 hours` 42, `>4 hours` 3), `version`, `created_at`, `FAIL_TO_PASS`, `PASS_TO_PASS`, and gold `patch`. Strata: repo group (django / sympy / sphinx / other) x (easy `<15 min` vs harder), proportional allocation, minimum 1 per cell, seeded draw (script in section 5). Median gold size in the candidate pool is 6 changed lines, 164 of 318 gold patches are 6 lines or fewer [M]; the over-engineering claim is tested on that easy/small subgroup as a pre-declared secondary.

**The pool is smaller than "327" [M].** `relay/selfimprove/burned.jsonl` has 232 ids, 182 of them inside Verified (80 Verified-fresh + 101 selfimprove A/B + 1 debugging). Verified minus those = 318. A further 50 ids in `.fleet/swe/_selfimprove_slice.txt` are in that 318 but unregistered (were they run? [U]); excluding them gives 268. The Lite-300 list is **not on disk** in the main checkout (`all300_spec.json` is referenced but absent) [M], and 93 Lite ids overlap Verified, so the clean pool is between about 175 and 268 [E] until the list is regenerated. Rule: reserve 60 problems for the later policy arm; **N = min(156, pool - 60)**; if the pool is under 216, report the minimum detectable effect that N buys (N=100, psi 0.20: about 54% power for 10 points [E]).

## 2. Metrics and where each comes from

| Metric | Source |
|---|---|
| Pass/fail | Grader verdict only: `swebench` report (`resolved_ids`) via `bench/swe_grade_swebench.py` into `.fleet/swe/grade_results.jsonl` (rows with EVALERR are not verdicts, `bench/verdicts.py`). Never the worker's `outcome`. |
| Patch size (lines) | `preds_<arm>/<inst>.json[0].model_patch` (captured `git diff` of the worktree). Changed lines = lines starting `+`/`-` excluding `+++`/`---`, counted over non-test paths; test-file lines reported separately (gold `patch` excludes tests). Gold = spec `patch`, same count. **Ratio = model / max(gold, 1)**; report median ratio and paired arm differences. Not bytes. |
| Turns | `.fleet/history.json` row `turn` for the worker (key `<run_started>#w<idx>`, fields `outcome`, `run_id`, `transcript`, `phase_events`); cross-check transcript `turn` field in `.fleet/transcripts/<run_id>_w<idx>.jsonl(.gz)`. Copy rows out per arm: history.json has been cleared repeatedly (`history.json.cleared-*`) [M]. |
| Refuter calls | `.fleet/mechanisms.jsonl` rows `mechanism=="refuter"`, `executed==true`, count per `run_id`, `extra.lenses`, `decision_after` (UPHELD/REFUTED). **Caution [M]:** 913 of 1,513 executed refuter rows have empty `instance`, so per-problem attribution from this file is not reliable; attribute per run, or per worker from transcripts (see section 5). |
| Wall time | Transcript first-to-last `ts` per worker; history `run_started` to `ts`. |
| Tab/worker-seconds | Worker wall x `tab_weight` (relay_fleet.py:4655; about 1 for min, about 3 for auto/ultra per the comment in swe_solve_decoupled.py). The weight is a reservation, not a measured occupancy [E]; measured side-page seconds are [U] and must be checked in the pilot from `phase_events`. |
| Effort in effect | section 3. |

## 3. How each arm runs, and the one safe effort mechanism

Existing harness for Verified: `bench/swe_solve_decoupled.py --spec <solve spec> --targets-file <ids> --effort <arm> --preds-dir preds_<arm>_r<k> --tag <arm>_r<k> --max-turns 50 --max-concurrent 2 --chunk 10` (docker-free acceptance: non-empty diff; the agent never sees hidden tests, unlike `swe_run_until_done.py`, whose `swe_check` feedback would leak grader signal), then `bench/swe_grade_swebench.py --preds-dir ... --dataset-name princeton-nlp/SWE-bench_Verified --max-workers N`. `scripts/win/run_effort_ab.ps1` is **not** usable: it drives the SWE-Pro slice through `submit_via_ui.ps1` and **writes `effort=` straight into settings.txt**, against the GUI rule. `tools/fleet_intake.fleet_submit` is also not usable: no effort or turn field, 4,000-character goal cap, and its duplicate filter (`DUP_SIMILARITY` 0.5) would refuse the same problem submitted for a second arm.

Three ways to set effort, per code:
1. `settings.txt effort=` (GUI): global; changes the owner's live fleet. Read only when no CLI value is given (fleet_runner.py:3452). Rejected.
2. Per-goal `"effort"` key (relay/effort.py, `relay_fleet._worker_for`): lets arms interleave in one run, **but** it maps `auto` to lenses `None` (generic reviewer), whereas run-level `auto` on a coding goal uses `rootcause_code` (fleet_runner.py:3484). Arms would not equal the ones the owner runs. Also needs a one-line change to `write_goals`. Rejected for the primary comparison.
3. **CLI `--effort <arm>` on `swe_solve_decoupled.py`: recommended.** Per process, touches no settings, so the owner's GUI value stays authoritative for production; the flag "beating the setting" is the intent here because it is per-process. One process per arm-repeat-block, each with its own preds dir and tag.

**Verify per arm, per run (abort and discard the block on mismatch).** The `effort` mechanism row records only numbers, not the name [M], so match signatures: `min` = refuter false, {max_refute 0, max_research 0}; `max` = {2, 3}, no lens; `auto` = {3, 3}, lens `rootcause_code` (272 and 337 such rows seen for the two middle levels [M]); `ultra` = {4, 6} with lenses correctness/edge/security. Sources: `effort`, `refuter`, `panel` rows for the run's `run_id` (`config_source: run`), the coordinator log line `[effort] <arm> (refuter=.. lenses=.. refute<=.. research<=..)` (tee'd to `<state_dir>/coordinator_*.log`), and, because production already runs `effort_policy=shadow` [M: rows with `mode_source: settings`, `mode: shadow`], the `effort_policy` `initial` row (`config_value` = level name). **Refuse to start if the mode is `on`** (it changes assignment and child effort). Snapshot the settings keys `effort, effort_policy, review_lens_count, maxtabs, autoscale*, disk floor` and their hash at each chunk start; a hash change mid-arm (owner edited via the GUI) marks that chunk tainted.

**Arm interleaving:** blocks of 20 problems; inside a block run min, max, auto, ultra (order randomised per block), repeat 1 then repeat 2 in a different block order. This spreads time-of-day, rate-limit and Copilot-side drift over all arms.

**Keeping the owner's production fleet unaffected.** One coordinator per state dir (`fleet_runner.lock`, `--wait-for-state-dir-seconds`), but the Copilot tabs, RAM and the request-rate ceiling are shared, and each coordinator's admission counts only its own tabs. So: run from a **dedicated checkout of a pinned commit** with its own `--state-dir` (also gives its own `.fleet/mechanisms.jsonl`, which is hard-wired relative to the repo, so bench rows never mix with production rows); never in the live tree. Start a block only when the production fleet is idle (`status.json` `running` false and `tasks/pending` empty); `--chunk 10` gives a preemption point every ~15 minutes; use `--max-concurrent 2`, not 3. Contention signals, checked between chunks: production pending-job age or count rising; bench solve minutes/instance above 2x the measured median 1.43 (section 4); C: free under 6 GB; pagefile growth; grading host load. Any signal pauses the bench, not production.

## 4. Capacity (existing history, read-only)

**Solve [M]:** `.fleet/swe/solve_decoupled_si{on,off}.log` (Sept 9-12, effort auto, conc 3, 50 turns): 9 chunks of 12-20 instances took 18.6-64.7 min, per-instance wall 0.93-3.23, **median 1.43 min/instance** (about 4.3 worker-minutes). Staging 1.2-5.3 min per 20 worktrees (about 0.15/inst). Ultra used 27% more turns in the n=6 A/B [M]; assume x1.1 blended. Solve = **1.72 min/run [E]**.
**Grade, remote host [M/E]:** Lite-300 batch of 258 finished after the 60-minute poll deadline, under the 120-minute ceiling (SCORECARD_swebench_lite300_strong.md), max_workers 12: **14-28 s per instance amortised [E]**. Its current status is not readable from here; the notes describe an eval host that has worked for Verified (80x2 graded, EVALERR 0) and then needed five debugging fixes on Sept 10 [M]. It is also the owner's production machine: grading there must be capped (workers, image pruning, floor on C:) and its config must not be touched [M, standing rule].
**Grade, local WSL2 [U/E]:** no timed local log exists in the artifacts I could read. Images are 2.8-7.7 GB each and pruned after every verdict (`swe_check` full prune, `SWE_KEEP_IMAGES=1` skips) [M]; cold sympy env build about 20 min [M]; concurrency must be 1 on the 16 GB machine [M]. Estimate **5-10 min/instance [E]**. Peak disk = one image, up to 7.7 GB. Free space was **0 bytes on C: while I ran the read-only checks for this document** (186 MB a minute later) [M], so local grading is not currently possible without freeing disk.

| Design | Runs | Solve @1.72 | Grade local (5-10 min) | Grade remote 12w (14-28 s) / 4w |
|---|---|---|---|---|
| Pilot 6x4x1 | 24 | 0.7 h | 2-4 h | 0.1-0.2 h |
| Stage B 60x4x2 | 480 | 13.8 h | 40-80 h (1.7-3.3 d) | 1.9-3.7 h / 5.6-11 h |
| Full 156x4x2 | 1,248 | **35.8 h** | **104-208 h (4.3-8.7 d)** | 4.9-9.7 h / 14.5-29 h |

Max instances per day, local, one at a time: 144-288 theoretical, **about 50-140 realistic** (owner's work, disk floor) [E]. **Binding constraint: grading, not Copilot.** Copilot is unlimited, but the fleet slot on this machine and the grader are not. Locally the full design takes 9-17 nights at 12 h/night. With the remote host at 4 workers, grading (15-29 h) is pipelined under the solve and the fleet time (36 h, about 3-4 nights at 12 h/night) becomes the limit. **Smallest design that still answers the question:** Stage B (N=60, 4 arms, 2 repeats, 480 runs): about 2-3 nights with remote grading. It can decide patch size and cost (paired, continuous, and the claimed effect is 10x [E]) but can only detect pass-rate differences of about 16 points or more (N=60, psi 0.20, 80% power [E]); it cannot show a 10-point effect. Identical patches across arms can be graded once by patch hash; how often that happens is [U].

## 5. Preconditions and risks

- **Fan-out accounting [M]:** `--fanout` defaults on. A parent that splits ends `done`/`FANOUT` after 1 turn (relay_fleet.py:5924, history row `outcome: FANOUT`); children and a later merge goal are separate workers. Rules: (1) outcome is the grader verdict on the worktree diff, never a worker outcome; a FANOUT parent is not a solve and not a failure; (2) cost = sum of all history rows whose goal text carries the problem's `wt_<inst>` path (parent + children + merge); (3) run the primary comparison with fan-out off and identical (needs a pass-through flag in `swe_solve_decoupled.py`, a small separate change), and report how many problems split in the pilot; (4) `refuter`/`panel`/`fanout` rows have no reliable `instance`, so join by `run_id`, one arm block per run.
- **Shadow rows:** with `effort_policy=shadow` in settings.txt, `effort_policy` rows are written per worker (`initial` and per turn, `executed: false`); use them as read-back and ignore them for cost. Mode `on` invalidates a block.
- **Leakage [M]:** (a) the spec holds gold `patch`, `test_patch`, `hints_text`, and worktrees live under `.fleet/swe/work/`, next to `gold_preds.json`, `_gold_p31.patch`, `all_raw.jsonl.gz`: a file-reading agent can climb to them. Run from the dedicated checkout whose `.fleet/swe` holds only a solve-spec (`instance_id, repo, base_commit, problem_statement`); keep the gold spec elsewhere and used only by the grader and the size script. (b) `swe_repos_setup_batch.py` clones the full history graph (blobless), so the upstream fix commit is reachable by `git log --all`. Fix: per-instance snapshot (`git archive` of `base_commit` into a fresh `git init`); whether this changes pass rate is [U]. Post-hoc probe: search transcripts for `git log|git show|git fetch|origin/` and for gold added-lines. (c) Public-benchmark memorisation is [U] and equal across arms, so it biases levels, not contrasts. (d) Agent-added test files can collide with the grader's `test_patch`; count them separately and check EVALERR reasons.
- **Contamination:** the burned list is the union of Lite-300 (needs the missing id list; the eval host caches the dataset), `burned.jsonl`, every `.fleet/swe/_*.txt`, and any `preds_*` directory. The check fails closed when the Lite list is absent. The draw seed is fixed and the drawn ids are committed before any run; the debugging set stays burned.

Executable guard (`--check` vets an ids file; `--draw N` prints the stratified set; not yet committed as code, since this change is documentation only):

```python
import argparse, collections, json, os, random, sys
ap = argparse.ArgumentParser()
ap.add_argument("--spec", required=True); ap.add_argument("--lite-ids", required=True)
ap.add_argument("--burned", default="relay/selfimprove/burned.jsonl")
ap.add_argument("--attempted-dirs", nargs="*", default=[]); ap.add_argument("--extra-lists", nargs="*", default=[])
ap.add_argument("--check"); ap.add_argument("--draw", type=int, default=0); ap.add_argument("--seed", type=int, default=20260930)
a = ap.parse_args()
if not os.path.isfile(a.lite_ids): sys.exit("REFUSED: no Lite-300 id list; a missing list is not an empty list")
lines = lambda p: {l.strip() for l in open(p, encoding="utf-8") if l.strip()}
burned = lines(a.lite_ids) | {json.loads(l)["instance_id"] for l in open(a.burned, encoding="utf-8") if l.strip()}
for p in a.extra_lists: burned |= lines(p)
for d in a.attempted_dirs: burned |= {f[:-5] for f in os.listdir(d) if f.endswith(".json")}
spec = {x["instance_id"]: x for x in json.load(open(a.spec, encoding="utf-8"))}
if a.check:
    bad = sorted(lines(a.check) & burned); print("burned:", bad or "none"); sys.exit(1 if bad else 0)
pool = sorted(i for i in spec if i not in burned)
grp = lambda r: r.split("/")[0] if r.split("/")[0] in ("django", "sympy", "sphinx-doc") else "other"
key = lambda i: (grp(spec[i]["repo"]), "easy" if spec[i]["difficulty"].startswith("<15") else "harder")
strata = collections.defaultdict(list)
for i in pool: strata[key(i)].append(i)
print("pool", len(pool), {"/".join(k): len(v) for k, v in sorted(strata.items())})
if a.draw:
    rng = random.Random(a.seed); out = []
    for k, v in sorted(strata.items()):
        rng.shuffle(v); out += v[:max(1, round(a.draw * len(v) / len(pool)))]
    print("\n".join(sorted(out)))
```

## 6. Run plan

1. Free C: (owner decision; measured 0 bytes today). Regenerate the Lite-300 id list; run the guard; fix the pool size; commit the drawn ids and the seed.
2. Create the dedicated checkout at a pinned commit, own `--state-dir`, solve-only spec, snapshot-repo staging, fan-out pass-through flag (each a separate reviewed change).
3. **Pilot: 6 problems (one per stratum type, from `--draw`) x 4 arms x 1 repeat = 24 runs.** Purpose only: minutes and disk per instance for solve and grade, accounting validation. No pass-rate claim. Checks: 24/24 preds present; arm signature matches for every run; parents/children/merge joined per problem; size ratios computed; EVALERR reasons; peak and minimum C: free; production queue age unchanged; number of splits; measured tab-seconds from `phase_events`.
4. **Scale-up rule.** Go to Stage B if all hold: signatures 24/24; EVALERR 0 (or re-gradable); grade time per instance keeps the projected grading total under 5 days (or remote host confirmed with the owner); C: free never under 4 GB; no production contention signal; splits under 10% or fully accounted. Otherwise stop and fix.
5. **Stage B** (N=60, 480 runs). Estimate psi and the paired size/cost effects from it. If the required N from the formula is at most pool minus 60, go to the full N; if not, stop at the minimum detectable effect the pool allows and say so.
6. **Full** in 20-problem blocks with the interleaving in section 3; poll grading per block; the policy arm runs only on the 60 held-back problems, after the uniform results are frozen.

## Claim ledger

- **Measured:** prior A/B numbers; solve timings (9 chunks); image size range and prune behaviour; burned.jsonl counts (232, 182 in Verified); pool 318/268; gold size distribution; missing Lite list; `effort` rows carry numbers only; refuter rows mostly lack `instance`; FANOUT parent record; `fleet_submit` limits; CLI beats settings; production shadow mode; C: at 0 bytes today.
- **Estimated:** power/N (psi is assumed), solve 1.72 min/run, all grading times, images/day, clean-pool range 175-268, ultra x1.1, tab-seconds from `tab_weight`.
- **Unknown:** local grade time per instance; remote host's current health and spare capacity; true psi with repeats; Lite-300 overlap with the A/B ids; whether agents read upstream history or memorised fixes; share of duplicate patches; how often problems fan out under each effort.
