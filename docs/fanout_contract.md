# Fan-out: who decides, what gets verified, and where the work goes

Written 2026-09-13, after items 3–5 of the codex plan. Everything here was measured on this
machine on that date; every claim names what was run. It exists so the same questions are not
re-investigated — if you are about to go read `fanout.py` to find out who decides a split, read
this first, then verify the one line you actually need.

---

## 1. The split decision is a staircase, and each step can say no

| step | who | when | says no by |
|---|---|---|---|
| capability | the run | launch | `--no-fanout`, or `FLEET_INTAKE_AUTOSTART_FANOUT=0` |
| triage | `relay/splittability.py` | `RelayWorker.__init__` | `NO_SPLIT` (confident) |
| verdict | the agent | turn 1 | `NO_SPLIT` marker in its reply |
| revision | the agent | mid-run | declining the mid-run offer |

**Capability is ON by default** (`--fanout` defaults true; `task_router._wants_fanout` returns
`True`). It used to be off, and the flag's own help text carried the reason — *"a goal that fits
should not pay for a split turn and a merge turn."* That was true while the flag **was** the
decision. It stopped being true when the per-goal judge landed:

```python
self.fanout = bool(fanout) and _depth0 and _goal_splittable
```

A goal that fits is judged `NO_SPLIT` and costs nothing, so the flag only decides whether the
question is ever **asked** — and off-by-default meant it was asked for nobody who did not know
to opt in. Turning the capability off is the only answer that cannot be revisited later: it
takes the question away from the judge for every goal in the run, including goals added mid-run
that nobody has seen yet.

**Triage is offline and is not the verdict.** `splittability.judge()` makes no live model call —
its own docstring says so — and returns `SPLIT` / `NO_SPLIT` / `UNCERTAIN`. `Verdict.should_split`
is `decision == SPLIT`, so `UNCERTAIN` used to resolve silently to "no": a regex ruleset settling
a question it documents itself as unable to settle. Now:

```python
_goal_splittable = bool(_v.should_split) or _d == _splittability.UNCERTAIN
```

`SPLIT` and `UNCERTAIN` spend one turn asking the agent. A confident `NO_SPLIT` spends nothing —
that is what keeps the default-on capability free.

**What that costs, measured 2026-09-13** over 805 distinct real goal texts from
`.fleet/**/transcripts/*.jsonl(.gz)` (SWE-bench harness prompts excluded):

| verdict | count | share | median chars |
|---|---|---|---|
| SPLIT | 12 | 1.5% | 471 |
| NO_SPLIT | 426 | 52.9% | 2368 |
| **UNCERTAIN** | **367** | **45.6%** | 3498 |

So the live judgement is not a rare edge — it is **47% of goals** (SPLIT + UNCERTAIN), each
paying one extra turn. Under the old rule it was 1.5%; routing every goal to the agent would be
100%, about 2.1× this. UNCERTAIN goals are also the longest by median, which is what one would
expect of the band the rules cannot settle.

That 47% is a real cost and it is the price of the change: the old behaviour spent no turn and
answered "no" for 45.6% of goals on a ruleset that documents itself as unable to decide them.

**The agent can decline.** `SPLIT_JOB` used to demand 2–12 subtasks with no other permitted
answer, so an agent handed one indivisible investigation had to invent a split or stall. It now
ends with an explicit `NO_SPLIT` option, and `fanout.declined_split(resp)` reads it.

- checked **before** `fanout_ready`, because a reply may name both markers (the prompt names
  them together);
- decided by whichever marker the reply **ended** on;
- a decline read as a ready split becomes an empty subtask list, which the worker handles as a
  *malformed* split — same work, opposite meaning in the record, and a deliberate refusal
  counted as a parse failure. Hence a branch of its own, and a `fanout_declined` metric.

## 2. The decision can be revised mid-run — `max_continue` is the trigger

`SPLIT_JOB` is pre-loaded in `__init__` and the split branch is gated on
`self.fanout and not self._fanout_done`, so before this change a split fired on turn 1 or never.
A goal that turned out mid-run not to fit had no way to say so.

The evidence already existed. `max_continue` (default **6**) counts consecutive replies that
report progress and never reach DONE. That is **not** a stalled worker — `no_progress` is the
counter for a *verbatim-identical* reply, and it is separate. Six different replies that each
keep going is a goal that does not fit in one conversation, which is exactly what fan-out is
for. The worker's previous answer to that evidence was `status, outcome = "stuck", "STUCK"`.

`RelayWorker._ask_for_a_midrun_split()` now runs first. It returns `False` — leaving the old
STUCK path byte-for-byte intact — unless all of:

- `self._fanout_capable` — the run is capable **and** this worker is depth 0 **and** a
  `spawn_fn` exists. Kept separately from `self.fanout`, which is the *verdict* taken before
  the work started; the whole point is that the verdict was made when nothing was known.
- not already split (`_fanout_done`), and not already offered (`_midrun_split_asked`).

The offer is made **once**. A second decline lets the continue counter run back up to the cap,
and that time the worker really does stop.

`fanout.midrun_split_job(n)` is separate text from `SPLIT_JOB`, not a variant for variety:

- `SPLIT_JOB` opens with 「実行はまだしないでください」, wrong for an agent six turns into the work;
- it says nothing about what is already finished, so an agent told only "divide this goal"
  re-divides the part it already did and the children redo it. The mid-run prompt requires the
  agent to state what is done first, then split only the remainder;
- the observed continue count is **in** the prompt, because it is the evidence. "You have gone
  six turns without finishing" is a fact the agent can weigh; "please split this" is an
  instruction it can only obey.

**The parent's own work travels with the split.** A worker splitting at turn seven has produced
seven turns of output, and `FANOUT` ends it — the merge reads child records only. So its partial
result goes into the campaign (`partial`), onto the ledger header, and reaches
`aggregation_goal(parent_partial=...)`, which prepends it as `subtask_index: 0, outcome: DONE`.
Slice 0 because it came first, and DONE because `missing_slices` only counts non-DONE records:
it adds material without moving the gap check. Losing six turns of real work to the mechanism
meant to rescue them would be worse than the failure it replaces.

## 3. A child is not judged by the whole goal

Measured 2026-09-13 — parent check `{"type": "pytest", "args": "-q tests/"}`, split three ways:

```
child 1/3  checks=[{'type': 'pytest', 'args': '-q tests/'}]
child 2/3  checks=[{'type': 'pytest', 'args': '-q tests/'}]
child 3/3  checks=[{'type': 'pytest', 'args': '-q tests/'}]
identical across children: True
```

…while each child's prompt, built by the same function, says
「他の範囲は別の会話が並行して担当しているので、手を出さないこと」. It failed in both directions:

- **strict check → never passes.** Child 1 finishes its slice, the suite still fails on 2 and 3,
  the gate reports `VERIFY_FAILED`, and the child spends its remaining turns being pushed at work
  it was told not to touch.
- **loose check → passes for free.** `file_exists: out/report.csv` becomes true the moment any
  sibling writes it, so a child that did nothing verifies — and `_salvage_via_checks` promotes
  that into a *salvaged DONE*.

There is no correct per-slice check to derive: a step is a sentence, and turning it into a shell
command would be a guess. What is correct is where the parent's check **belongs**.

> **`child_goals()` no longer takes `checks`.** The parameter was removed rather than ignored —
> a caller that still holds a whole-goal check must be made to say where it goes. Silently
> dropping it would leave that caller believing its children are still verified, which is the
> state this was in.

The parent's check travels the route `cwd` already takes:

```
_spawn_children(parent_checks=…) → campaigns[cid]["checks"] → ledger header "checks"
                                 → campaigns_from_ledger → aggregation_goal(parent_checks=…)
```

The merge **is** the parent goal finishing, in the parent's cwd. That is the worker for which
"did the whole goal succeed" is the right question.

### 3a. So a child now has no acceptance check at all. Is that a downgrade?

It is an honest one, and the tri-state was built for exactly this. `verified` is
`None = nothing was configured to verify`, `True/False = a gate ran`. A child used to end
`True` or `False` from a gate that was **measuring the wrong thing** — the whole goal — so both
values were wrong: a `True` meant a sibling had satisfied it, a `False` meant siblings were not
finished yet. `None` is the accurate state, and it is distinguishable in the telemetry, which is
why that distinction was built (measured 2026-09-09: 67 of 68 `verified=False` rows had
`verify_attempts=0`, i.e. no gate had run).

The consequence is that a child's DONE is taken on trust and the **merge** is where the goal is
actually verified — which is the only place the whole-goal check can be answered. If a child
lied, the merge's own check fails and the merge iterates. Under the old arrangement the child
"verified" on its sibling's work and the merge verified nothing at all.

`_salvage_via_checks` on a child is now a no-op (`if not self.checks: return False`), and that
removes a false rescue rather than a real one: the loose-check case above is precisely a child
being salvaged to DONE on an artifact a sibling wrote.

## 4. `reply_contains` — a check about the answer, and why it had to exist

`merge_acceptance_checks` used to return bare **strings**. `acceptance.normalize_checks`
"silently drops non-dict members". Measured:

```
merge_acceptance_checks(records) -> ['未取得または未完了のサブタスク 2 について、…']
types                            -> ['str']
aggregation_goal(...)['checks']  -> [that string]
goal_fields(...) checks          -> []          <-- dropped
```

So the worker took `if not self.checks` — *"no checks → DONE accepted as before (back-compat
trust)"* — and **the one gate standing between a merge and a confident report of an incomplete
sweep had never run.** Four tests in `test_merge_delivery.py` asserted the goal *carried* the
checks; none asked whether anything *read* them. A check on the producer is not a check on the
pipeline.

Every other check type asks the **workspace** a question. "Does this report name the slices it
could not get" is true or false in the **text** and nowhere else, so expressing it as a shell
check meant not expressing it at all. Hence a new type:

```python
{"type": "reply_contains", "needle": "未取得"}
{"type": "reply_contains", "all_of": ["2", "4"]}
{"type": "reply_contains", "needle": "欠落なし", "expect": false}   # must NOT appear
```

- resolves instantly at `start()`, like the file types — no process;
- `Check(spec, reply=…)`; the reply is threaded in from `_on_done_claimed(resp)`, which is where
  the claim arrives (`resp` was a local and went out of scope);
- `reply is None` → **fails**. `_salvage_via_checks` runs checks with no DONE reply in hand, and
  answering "passed" there would accept the very claim the check exists to test;
- an empty needle list → fails. A check that cannot fail reads in a log exactly like one that
  ran and was satisfied.

The merge gate is three checks because the recorded incident has three faces — two merges that
ended DONE having written 「欠落なし」 with slices missing:

| check | catches |
|---|---|
| `all_of: [gap numbers]` | the gaps are not named |
| `needle: 未取得` | the word the merge prompt itself demands is absent |
| `needle: 欠落なし, expect: false` | **the sentence actually observed** |

The number check is **lenient by construction** and that is deliberate: a bare `"2"` also matches
inside `"812"`, so it can pass on a coincidence — measured, and pinned by
`test_the_number_check_alone_would_not_have_caught_it`. It cannot *fail* on one, which is the
direction that matters: it never blocks a correct report, and the other two carry the strictness.

## 5. One goal splits once

`campaign_id_for()` hashes the parent goal so a resumed goal lands in the same campaign. Landing
there never came to mean **joining** it: `_spawn_children` overwrote `campaigns[cid]` and
re-queued every child. Measured over `.fleet/campaigns.jsonl`:

```
campaigns whose header was written more than once : 10
campaigns carrying a repeated subtask_index       : 15
c7e01b58b1956                                     : 22 headers, 128 children (a 7-way split)
```

`self._fanout_done` never covered this and was not meant to — it stops **one worker** splitting
twice. `campaigns` is local to a call of `run_relay_fleet` and starts empty, so a resumed run, a
retried goal, or a second worker on the same text all arrive with no memory of the first split.

Two guards: `cid in campaigns` (this run) and `_campaign_already_on_disk(cid)` (an earlier run,
read from the ledger's **header** lines — a child row proves children were queued, a header proves
a *family* was declared). An unreadable ledger answers **True**: refusing to split is recoverable,
splitting twice is not.

> **This unsettles an old conclusion.** `c7e01b58b1956` is the campaign this repository cites as
> the over-split case — "4 of 7 subtasks refused, starved of the context the others held" — and
> that observation is why the length proxy was replaced by the independence judge. With the same
> split run twenty-two times, those refusals have a second explanation. The change does not
> settle which; it removes the mechanism that made the question ambiguous.

## 5a. A family survives the process that split it — as of 2026-09-13, and not before

`campaigns_from_ledger` is 43 lines, tested, exported, and was written for exactly one case:

> On FleetContextLost the fleet re-enters `run_relay_fleet` with a fresh process, so the
> in-memory `campaigns` dict is empty and `_unfinished()` returns only goals — never families.

**Nothing called it.** Repo-wide grep found no caller outside tests, and this repository's own
unreached inventory had carried `relay/fanout.py::campaigns_from_ledger  # 43 lines` in
`NO_CALLER_BUT_TESTED` since the day it was written. A test referencing a function says it was
worth writing; it says nothing about anything reaching it.

It got worse the same day it was found: the header now carries `checks` and `partial`, which
exist **only** on disk once the process is gone.

Wired now. `run_relay_fleet` starts with `campaigns = _campaigns_from_disk(transcript_dir)`, and
the merge writes `{"kind": "merged", "campaign_id": …}` when it queues — because `merged` used
to live only in memory, so rehydration alone would have re-delivered every past merge. Families
already merged are dropped at rehydration; a family whose children are not in this run is inert,
because `ready_to_aggregate([])` is False by design.

## 6. A count without a time axis says nothing about the present

Recorded because the same mistake was made three times in one day, each time from a ledger total:

| observation | first reading | actual |
|---|---|---|
| `verified=False` × 67 | acceptance is broken | residue from before the 09-09 tri-state fix; zero since |
| orphan campaigns × 60 | parents cannot be recovered | all before the header line existed; zero since |
| `REFUSED` × 77 (29%) | refusal is the norm | 71 of them on one day; other days 0–7% |

**Rule: any count taken from `.fleet` is divided by date or line order before it is reported.** A
distribution clustered in one place is an *event*, not a *state*. The duplicate-header count above
survives that test — it runs from line 424 to line 816 of an 882-line ledger, with four of the ten
having their last duplicate past line 700 — which is why it was treated as live.

## 7. Where the prompt budget is, and why the mid-run offer is not in it

`PROTOCOL` has a hard budget of **1500 characters** (`relay/test_instruction_budget.py`), and the
failure message states the discipline: *足すのではなく、既存の1文を置換するか、「関連する瞬間」へ
移すこと*. Teaching every worker about mid-run splitting up front would spend that budget on every
turn of every goal. The offer is therefore made **at the moment it applies**, in the nudge that
replaces the STUCK ending — zero steady-state cost.

## 7a. Known limits — what this does NOT establish

Raised by an adversarial review (codex `gpt-6-astra`, 2026-09-13). Recorded because the next
person will otherwise rediscover them, and because two of them are genuinely open.

**`reply_contains` checks vocabulary, not facts.** It verifies a lexical property of the reply.
A reply can satisfy all three merge checks and still be wrong — *"Examples of subtask numbers
include 2 and 4. 未取得 means unavailable. All requested work is complete."* — and the negative
check can reject a *truthful correction* that quotes the forbidden phrase (*"前回の「欠落なし」は
誤りです。2、4は未取得です"*). It is a false **reject**, not a false accept, so it costs a turn
rather than an answer. The strict version is to compute the gap status in the orchestrator and
have the merge reproduce a structured field, which is a larger change and is not done.

**`max_continue` is elapsed effort, not evidence of independence.** A sequential migration where
each step depends on inspecting the last, or a single race condition being narrowed by
successive experiments, can run six honest turns with nothing parallelizable. The agent can
decline, and the transition on decline is defined: the worker carries on with a **fresh continue
budget**, once — `_midrun_split_asked` is already set, so the second time the counter trips it
goes STUCK as it always did. In effect a declining depth-0 worker gets `max_continue` twice.
That is deliberate ("I can finish this") but it is a doubling, and it is the reason the offer is
made only once.

**Concurrent mutation of one tree is not addressed here.** Children share the parent's `cwd` by
design. A goal that *looks* divisible but shares files — "rename this API across implementation,
callers and tests" — can have siblings overwrite each other. The independence judge and the
agent's consent are both opinions about the goal TEXT; neither establishes disjoint write
scopes. Default-on fan-out widens the exposure to this. Not solved; not caused by this change.

**The duplicate-header cause is not identified.** §5 measures repeated writes; it does not say
whether retries, concurrent writers, or recovery replay produced them. The guard is
check-then-append, which is not atomic across processes, so it closes the single-writer case
only. To settle it: record run/process ids and campaign-creation attempts, and compare the
ordering of ledger persistence against child dispatch.

**The campaign id is a content hash, not an execution identity.** A deliberate re-run of a
completed goal now splits again (a merged family is not adopted — see §5a), but an **unmerged
abandoned** campaign still suppresses a fresh run of the same text. Separating "resume this
execution" from "run this goal again" needs a persisted execution id.

**Merge delivery is at-least-once, not exactly-once.** A crash between queueing the merge and
appending its `merged` note causes redelivery; reversing the order would risk losing it. Chosen
deliberately in that direction — a duplicate answer is visible, a missing one is not.

## 8. Things deliberately not done

- **A per-slice check derived from the step text.** It would be a guess, and a guessed oracle is
  worse than none.
- **An LLM call inside `splittability.judge()`.** The module is pure and offline on purpose —
  fast, unit-testable, no network in the hot path. The live judgement belongs to the agent, which
  is already in a conversation and already reading the goal.
- **Recursive splitting.** `MAX_DEPTH = 1`, and `_fanout_capable` is false for any child, so a
  mid-run split cannot produce grandchildren either.
