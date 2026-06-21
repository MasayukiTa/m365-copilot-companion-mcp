# Open-ended continual self-growth (L2 -> L4) — design

Date: 2026-06-21
Extends: bench/SELF_IMPROVEMENT_CONTROLLER.md (L1 is built + dogfooded).

Goal: take the controller from "a human kicks one gated iteration" (L1, done) to a system that
**keeps improving itself, unattended, without quietly cheating or drifting**. The hard part is not
the loop — it is the discipline that keeps the loop *honest* as it runs for days. This doc is mostly
about that.

> Interpretation note: "openclaw" is read here as the class of continuously self-improving coding
> agents (SICA arXiv:2504.15228; Darwin-Gödel-Machine-style archives; AlphaEvolve-style
> propose/evaluate). If a specific system was meant, the mechanisms below still map onto it.

## 0. The one failure that dominates everything: reward hacking

A self-improving agent optimised against a fixed grader will, given enough iterations, find ways to
raise the *number* without raising the *capability*. On SWE-bench specifically:

- editing the harness/grader to pass (the eval, the gold tests, the `report.json` parse)
- editing the guards so the significance gate always says "keep"
- editing the burned registry to re-use easy, already-seen instances
- skipping the hidden tests, or weakening the diff-gate so empty patches "pass"
- prompt-injecting the grader, or writing to the FAIL_TO_PASS files
- overfitting the scaffold to the *measured* slice (defeated only by held-out + burned hygiene)

**Design rule: the agent may edit the SCAFFOLD; it may NEVER edit the JUDGE.** Everything that
defines success or enforces honesty is a *frozen constitution* the self-improvement process cannot
modify. Concretely, the following are write-protected from the loop and any agent it spawns:

- the grader (`bench/swe_grade_swebench.py`, `the eval host_batch_grade.py`) and the swebench harness on the eval host
- the gold/test data, the dataset parquet, the `report.json` -> verdict mapping
- the guards that ENFORCE honesty: `significance_gate`, `BurnedRegistry`, `overfit_lint`,
  `classify_outcome`
- this constitution doc and the frozen-set manifest itself

Enforcement (not just convention): grading runs on a separate host (the eval host) the solver cannot reach;
the frozen set is checksummed before each iteration and the loop **aborts if any frozen file
changed**; the solver worktrees never contain the gold patch or test_patch; cross-dataset
confirmation (below) catches gains that are grader-specific rather than real.

## 1. Autonomy ladder

- **L1 (built)** — human kicks one iteration; reviews keep/revert. `relay/selfimprove/loop.py`.
- **L2** — `CronCreate` schedules iterations; auto-commits ONLY changes that pass the gate AND leave
  the frozen set intact; everything else queues for human review. Per-iteration spend ceiling.
- **L3** — L2 + automatic held-out rotation and dataset expansion when the fresh pool runs low; hard
  stop + page-human when K iterations yield no significant gain (plateau) or any tripwire fires.
- **L4 (open-ended)** — the PROPOSE step becomes generative (invents new scaffold mechanisms, not
  just card tweaks); a quality-diversity archive sustains exploration; the agent can self-repair its
  own *harness* (not the judge); meta-improvement of the proposer/guards under a frozen core.

## 2. Archive (the genome)

Move from "current scaffold + linear keep/revert" to a **versioned archive of scaffold genomes**, so
improvement can branch and build on the best ancestor rather than a single mutable HEAD (the
Darwin-Gödel / SICA insight — a linear chain gets stuck; an archive lets a later iteration revive a
dormant-but-promising branch).

- a *genome* = the set of env-gated scaffold knobs + card texts + their defaults (a serialisable
  diff over the frozen base).
- each archive entry: genome, the fresh slice it was validated on (now burned), pass@1 + CI, the
  gate verdict, parent id, and behaviour descriptors (below).
- selection: build the next proposal on a parent chosen from the archive (best, or a
  quality-diversity cell), not always on HEAD.

## 3. Open-ended PROPOSE (L4)

L1's "propose" is implicit (a human writes a card). L4 makes it a **generative meta-agent** that, given
the partitioned *real* misses (infra filtered out) and the archive, proposes candidate genomes:

- not only new card texts, but new *mechanisms*: a new self-test shape, a new tool the solver may
  call, a new retrieval step, a different turn budget policy.
- every proposal passes `overfit_lint` (no instance/repo/file/test names) BEFORE it is allowed to be
  validated — domain-general or it does not run.
- diversity requirement: a proposal too similar (genome-distance) to an archived-and-rejected one is
  suppressed (anti ideation-fatigue; SICA's explicit lesson).

## 4. Quality-diversity, not just hill-climbing (sustaining open-endedness)

Pure "keep iff pass@1 up" plateaus and overfits. Maintain a **MAP-Elites-style archive** over
behaviour descriptors — e.g. {avg diff size, avg turns used, % of misses that are precision vs
wrong-layer, repo-family profile} — keeping the best genome per cell. This:

- preserves stylistically different solvers (small-surgical vs broad-refactor) instead of collapsing
  to one, which keeps exploration alive and guards against a single overfit mode;
- lets the curriculum (below) pull from whichever cell suits a new dataset.

## 5. Curriculum & held-out integrity

- fresh pool rotation: Verified fresh (407, minus burned) -> when low, expand to SWE-bench full,
  then Multi-SWE / other languages, then live GitHub issues. Each is held-out: an instance is graded
  at most once per genome and then burned.
- **cross-dataset confirmation**: a genome that wins on slice A must not REGRESS on a small fixed
  *sentinel* set drawn from a different distribution. A gain that appears on A but not the sentinel
  is flagged as likely grader/dataset-specific (possible reward-hacking) and NOT kept. (This is the
  general form of project_swe_eval_host_confound — confounds masquerade as gains.)

## 6. Self-repair of the harness (the L4 capability the orphan-death exposed)

This session, a *new* infra failure (detached-from-detached orphaning) was diagnosed and fixed by a
human. For L4, the loop must do that itself, for harness bugs only:

- `classify_outcome` already separates infra faults from real misses. When infra faults spike, the
  loop spawns a **repair sub-agent** scoped to the HARNESS (solve/launch/monitor code) — never the
  judge or the frozen set.
- the repair must come with a regression test that reproduces the fault and then passes; it is
  applied behind the same gate discipline (it must not change any score, only reduce infra faults).
- a repair that touches a frozen file is rejected outright.

This is the boundary between "human-supervised" (today) and "self-growing" (L4): the system extends
its own *hands* but never its own *judge*.

## 7. Meta-self-improvement (improving the improver) — bounded

L4 may also improve the PROPOSE meta-agent and the *advisory* guards (e.g. a better miss-clusterer).
But the **enforcing** guards (section 0 frozen set) are off-limits. Split the guards into:

- *advisory* (clusterer, proposer heuristics, behaviour descriptors) — self-improvable.
- *enforcing* (significance gate, burned registry, overfit linter, infra classifier, frozen-set
  checksum) — constitutional, human-only.

A meta-change is itself validated by the same loop: does the new proposer produce genomes that clear
the (frozen) gate more often on held-out data? If not, revert.

## 8. Tripwires & economics (when to stop / page a human)

- spend ceiling per iteration and per day (the budget mechanism); hard stop at the ceiling.
- plateau: K consecutive iterations with no gate-passing genome -> pause + report (don't burn compute
  chasing noise).
- anomaly tripwires that page a human and halt: frozen-set checksum changed; pass@1 jumps
  implausibly (>X pp in one step — usually a leak/hack, not real); infra-fault rate spikes;
  cross-dataset sentinel regresses; the agent attempts to write outside the scaffold allowlist.
- every kept change is a commit with its A/B evidence; every iteration is fully replayable.

## 9. Honest gap assessment

Buildable now on this substrate: L2 (cron + gated auto-commit + frozen-set checksum), L3 (rotation +
plateau stop + tripwires), the archive, cross-dataset sentinel, and self-repair-of-harness for the
*known* infra-fault classes. These are engineering.

Genuinely research-open (don't oversell): an open-ended PROPOSE that reliably *invents* useful new
mechanisms (vs tweaks) is not a solved problem; quality-diversity descriptors for "coding scaffold
behaviour" are a design bet; and sustained gains eventually need *model* improvement, not just
scaffold — scaffold self-improvement has a ceiling for a fixed model (the +6.2pp/p=0.18 result is a
reminder that single-knob gains are already small and need large N to even detect). True open-ended
growth that keeps compounding is, today, an aspiration with real diminishing returns — the value of
this design is that it can chase those returns *safely and honestly*, which is the part most
self-improvement attempts get wrong.

## Build order

1. L2: `CronCreate` driver + frozen-set checksum guard + per-iteration spend ceiling. (task #24)
2. Archive + behaviour descriptors + build-on-parent selection. (new)
3. Cross-dataset sentinel + the implausible-jump / frozen-changed tripwires. (new)
4. Generative PROPOSE meta-agent behind `overfit_lint` + diversity suppression. (new)
5. Self-repair-of-harness sub-agent (infra-faults only, frozen set off-limits). (new)
6. Meta-improvement of advisory guards under the constitutional core. (new)
