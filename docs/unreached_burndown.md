# Burning down the unreached baseline

Started 2026-09-13. **93 → 75** so far, and the scan now sees 77 — two names it had never printed at all. This file exists so they do not have to be
triaged a third time.

## Why the baseline is being removed rather than maintained

`tools/unreached.py` finds module-level public functions with no caller.
`tools/test_nothing_new_is_built_without_a_caller.py` froze the ones that existed into two sets,
so a *new* one fails CI and an *existing* one passes forever.

That ratchet stopped the problem growing and made every instance in it permanently invisible.
The cost is measured: `relay/fanout.py::campaigns_from_ledger` — 43 lines, exported, fully
unit-tested — sat in that baseline for weeks while the crash-recovery path its own docstring
names was broken. A family split before a crash was never merged again; its children could all
finish and the answer they were collected for was never assembled, silently. It was found by a
person reading code, not by the checker built to find exactly that.

An external analysis (DeepSeek's `dsh`) makes the same point from the other side: it enumerates
every uncovered location on **every run** and permits an exemption only with a written reason
plus a test proving the exemption still selects a real target. The difference is not the AST —
this repository already used one — it is what CI does with the finding.

## The instrument was wrong first

An inventory with false entries cannot justify deleting anything, so the scanner was fixed
before the list was acted on. Three blind spots, all measured:

| blind spot | how it showed up | fix |
|---|---|---|
| aliased import | `from relay.selfimprove.diversify import diversify as _diversify` then `_diversify(base, n)` — live production code listed as unreached | credit the original name when the **alias is called** |
| non-Python caller | `edge_keeper.ps1` runs `python -c "from relay.edge_recover import keeper_profile_marker as k; print(k())"` | a tracked `.ps1`/`.bat` naming **both the function and its module, in the same file** |
| registry decorator | `@mcp.custom_route("/health", …)` puts `main.py::health` in Starlette's routing table; no Python line names it again | a decorator that is a **Call** counts as a registration; a bare `@property` does not |
| **name defined twice** | adding a `require` to `relay/invariants.py` silently removed `relay/selfimprove/autonomy.py::require` — the hard autonomy gate — from the inventory: a colliding name was dropped from the scan entirely | a **zero** reference count resolves the ambiguity without resolving the name (no definition is reached, whichever one a call meant), so every place is reported; only a non-zero count is still skipped |

**Two of those fixes were themselves wrong first, and that is the lesson worth keeping.**

- Crediting every *reference* to an alias hid `harness_tree.py::branches`, because
  `from relay.selfimprove import branches as BR` imports a **module** of the same name.
- Crediting any `\bname\b` in a `.ps1` wrongly cleared three of six: `require` ("a step may
  require approval"), `branches` (`git branches`), `health` (a health-check script).

`require` is the one that mattered: the hard autonomy gate meant to sit at every mutating sink,
flagged unwired by an adversarial review hours earlier, nearly hidden behind an English word.

> **A false "reached" is worse than a false "unreached".** The row disappears, and nobody reads
> what is not printed. When tightening this tool, prefer noise over silence.

## What left the baseline, and why

| entry | reason |
|---|---|
| `relay/selfimprove/diversify.py::diversify` | false positive — aliased call |
| `bench/routing_switch.py::broker` | false positive — aliased call |
| `bench/ui_goal_lines.py::write_ui_file` | false positive — `.ps1` caller |
| `relay/edge_recover.py::keeper_profile_marker` | **fixed** — the `.ps1` now reads it instead of duplicating the list |
| `main.py::health` | false positive — route decorator |
| `bench/verdicts.py::is_resolved` | **fixed** — 12 literal comparisons in 7 files now call it |
| `tools/contract_gate.py::activate_contract` | **fixed** — an operator CLI can now turn the gate on |
| `relay/outcomes.py::is_retryable` | **fixed** — the retry decision goes through it; an unknown outcome is now printed and recorded as an omission rather than answered "no" |
| `relay/transport_policy.py::duplicate_risk` | **fixed** — `relay_fleet.py:5231` called the predicate instead of inlining its body |
| `relay/conv_title.py::salvageable` / `disambiguate` | **fixed** — a seven-way split registered 7 rows with 1 distinct title between them; the de-duplication pass now runs at registration (7/7) |
| `relay/selfimprove/runtime_config.py::revert_active` / `reset_to_base` / `pending_swap` | **fixed** — activation was wired and rollback was not; an operator CLI now reaches all three |
| `relay/fleet_retention.py::open_maybe_gz` | **fixed** — the prediction held: `fleet_reconcile.load_completions` globbed only `*.jsonl` and saw **2 of 1557** transcripts (692 goals after the fix) |
| `relay/profile_token.py::forget_memo` | **fixed** — `reset_socket_route`'s docstring asserted "not the token" while the token survived in profile_token's own `_MEMO`; it clears it now |
| `tools/lock_state.py::token_gap` | **fixed** — the counter that decides whether `MCP_REQUIRE_UNLOCK_TOKEN` can be enforced had gone unread for 26 days. Measured on the live file: **154** token-less calls since 2026-08-18, the newest that same morning, **146 of them one caller** — so enforcement would have refused the live integration. Two readers now: `python -m tools.lock_state token-gap`, and a line the server prints at startup while there is something to say |
| `relay/mechanism_telemetry.py::patch_hash` | **fixed** — `bench/pro_capture.py::_emit` computed the same digest inline, in a function whose own docstring argues against exactly that ("written twice, these drift"). One implementation now |

The ratchet noticed `is_resolved` on its own: the moment it gained callers the test refused to
keep it listed. That is the mechanism working in the direction it was built for.

## The remaining 77

Classified 2026-09-13 by three parallel surveys, each required to give grep-level evidence and
to answer "could not determine" rather than guess. **These verdicts are triage, not proof** —
the ones acted on so far were re-verified by hand first, and the rest should be too.

### The heaviest UNWIRED — a call site exists and is missing

**Empty, for the first time.** Both entries were wired on 2026-09-13 — `lock_state::token_gap`
and `mechanism_telemetry::patch_hash` — and they are listed under *What left the baseline*
above. A name belongs here when a caller for it exists somewhere in the design and is simply
missing; that is the shape worth doing next, so the heading stays.

### Deliberately unwired — do not "fix"

`relay/selfimprove/autonomy.py::raise_to` — its docstring says so, and
`test_autonomy.py::test_raise_to_has_no_production_caller` **fails the build if a caller
appears**. A single-machine agent raising its own privilege would be a stub that looks like a
control. Leave it.

### The self-improvement subsystem has no driver

23 of the 77 are in `relay/selfimprove/` — the share has GROWN as the rest came down. `scripts/run_nightly_real.py` — the script meant to
run the loop for real — opens with *"It has never been run at all."* No CI job, scheduler,
`.bat` or cron invokes any entry point. They are stranded because the loop was never turned on,
not because they are useless. **Decide that first**; classifying them one by one before the
subsystem's fate is decided is wasted work.

## How to finish this

1. Take one entry. Re-verify the triage above by hand — it is evidence, not a verdict.
2. DEAD → delete it, and delete its tests.
3. UNWIRED → wire it **and add a test that enters at the production seam**, not at the helper.
   Testing the parser is how `campaigns_from_ledger` passed for weeks.
4. Scanner-wrong → fix the scanner, as above; an exemption is the last resort, not the first.
5. Remove the entry from the baseline. The ratchet will tell you if you were wrong.
6. When the list reaches zero, remove the grandfathering: after that, a new unreached function
   is a failure rather than an addition to an inventory.

### `relay/execution_profiles.py::validate_runtime` — its INPUT does not exist

Reported as an unwired validation: `local_job_store.create_job` resolves a profile and never
calls it, so a job can be accepted as LOCAL_LOOP with no local MCP and the `RoutingError` never
fires. The first half is true. The second is not actionable as a wiring:

```
grep capabilities / local_mcp_available / workiq_available  ->  only execution_profiles.py
resolve_profile(job, capabilities=None)  ->  every caller passes nothing (`capabilities or {}`)
```

Nothing in the repository ever BUILDS a capabilities dict. Wiring `validate_runtime` means
deciding how to answer "is local MCP available" — a probe nobody has designed, with its own
failure modes (what does an unreachable probe mean: unavailable, or unknown?). Inventing one
here would be choosing a policy and presenting it as a wiring.

This is an unbuilt feature, not a missing call. Whoever builds the capability probe wires this
at the same time; until then the entry stays, and the reason is written here rather than
rediscovered.

## The same question, asked of the records instead of the source

`tools/unreached.py` asks "was this written and never called?" of the source. On 2026-09-13 the
same question was asked of the live ledgers for the first time — a key that every row carries and
no row ever fills is an instrument with no value in exactly the way an uncalled function is, and
harder to see, because the file is there, the rows are there and the column is there. Only the
answer is not.

`tools/ledger_health.py` is the scanner (`python -m tools.ledger_health`), and
`tools/test_a_declared_field_is_a_written_field.py` is the ratchet, with the same two halves as
the unreached one: nothing new may go unwritten, and a column that starts being written has to
come off the list. It skips where there is no `.fleet`, so it runs on the machine that has the
records and not in CI — the same honesty as `ui/test_deployed_cockpit_matches_its_source.py`.

First survey, over `.fleet/*.jsonl`:

```
mechanisms.jsonl  5629 rows   artifact_hash, attempt, execution_error, goal_hash
judge.jsonl       9361 rows   human_approved
```

Two of the five were live defects rather than unused columns.

### `goal_hash` — two telemetry rungs that had never once been written

`relay/mechanism_telemetry` was imported **locally at eight call sites** in `relay_fleet.py` and
bound nowhere at module level. Two sites used `_mt` with no import in scope:

| line | what it records |
|---|---|
| 2423, `RelayWorker.__init__` | "fan-out is eligible for THIS goal" — step two of the staircase, per worker |
| 6140, `_spawn_children` | "this goal was actually split" |

Both raised `NameError` into an `except Exception: pass`, which is indistinguishable from *the
branch was not taken*. Measured in the data: all 339 fan-out rows carry `config_source="run"`,
and **zero** carry `"per-goal judge"` or `"split"`. So for the whole life of the log the fan-out
telemetry has said that fan-out was *configured* and has never once said that it *did anything* —
and any reading of whether fan-out works has been made without its two middle rungs.

The repository already knew this failure mode; the comment above the seventh local import says
so:

> ... except below and the record would silently never be written — the same silence this
> function exists to end, reintroduced inside it.

Which is why the fix is one module-level binding and
`relay/test_a_swallowed_record_is_no_record.py`, not two more local imports: a name that has to
be repeated at every call site gets missed at some call site, and the bare except guarantees
nobody finds out. The test fails on any `_mt.record(` whose `_mt` is not bound in an enclosing
scope, so the class is closed rather than the two instances.

### And the isolation that guarded the tests did not reach twenty of them

Finding `goal_hash` cost one more, because fixing it made the writes real. A preflight run
appended **67 rows to the operator's live `.fleet/mechanisms.jsonl`** — per-goal fan-out
judgements with an empty `run_id`, timestamped inside the script-style phase.

`relay/test_live_record_isolation.py` and conftest's autouse fixture exist precisely so a test
cannot write where the operator's records accumulate. `scripts/run_script_style_tests.py` runs
twenty files that pytest collects nothing from — as plain scripts, in their own interpreter —
so conftest never runs for them, and **nothing had ever stopped them writing to the real
`.fleet`**: not just telemetry, every record module those suites touch, for as long as the
runner has existed. The guard, the table and the classification were all in place and none of
it reached the suites covering the relay loop, the planner, the watchdog, transient retry and
the unlock injection path.

It surfaced only because the write had been failing. That is the general shape worth keeping:
**a leak behind a broken writer is invisible, and fixing the writer is what exposes it.** The
instinct to treat the new rows as a regression in the fix was wrong — the fix is what made an
old hole measurable.

`scripts/run_isolated.py` now applies `conftest.LIVE_RECORD_REDIRECTS` — the same table, not a
second copy — before exec'ing each suite. Verified by measurement rather than by reading: 20
suites still green, live file unchanged at 5739 lines.

### `human_approved` — the same hole, seen from the other end

Empty on all 9361 rows of `judge.jsonl` because `tools/judge_backend.py::ask_human_async` has no
caller: no judgement has ever been put to a person. That function has been sitting in the
unreached inventory the whole time. One hole, two symptoms, and neither list could see the other
until both existed.

## The opposite of a baseline: post-conditions checked while the code runs

The external analysis this burn-down keeps citing named two things worth taking, and both are
now in. Its own framing of the second one is the sharpest description of what a baseline does
wrong:

> ベースライン腐敗の本質は「壊れた状態が検証されず静かに緑のまま残る」こと。
> invariants は状態を許容リストで黙らせず、毎回の実行時に検証し違反を例外で叫ぶ。
> ベースライン固定の思想的対極。

*(The quotation names this repository by its internal name; that word is not carried into this
file. The rule is written down in `scripts/check_no_identifying_names.py`, and it caught this
paragraph before it was committed — the third time content read outside the repository has been
transcribed into a tracked one.)*

`relay/invariants.py` is the port it asked for — explicitly the minimum: *"a thin layer that
asserts post-conditions on important paths and raises a machine-readable code;
`assert_invariant(name, cond, msg)` is enough."* Violations carry `INVARIANT` and the owning
module, and land in `.fleet/invariants.jsonl` before anything is raised, so a violation swallowed
three frames up is still on disk.

**One difference from the source, deliberately.** `dsh` raises on every violation. Here a raise
on the fleet's hot path costs a fifty-minute run, and *telemetry must not be able to fail a run*
is a rule this repository has already paid for. So each invariant declares its disposition:

| | when | first one |
|---|---|---|
| `RAISE` | a wrong answer is worse than a stop | `reconciler.transcripts_are_readable` — the reader that matched 1557 transcripts and returned 2 |
| `RECORD` | the caller is recovering and stopping is worse | `socket_route.reset_keeps_no_token` — the docstring that claimed the token was gone while it was not |

`python -m relay.invariants list` prints what the repository promises at runtime;
`violations` prints what has stopped being true.

**The limit is the source's own**, and it is why `relay/test_an_invariant_has_a_call_site.py`
exists: *"if nobody writes the invariants, nothing is protected."* A registry of promises
nothing checks would be this same burn-down's defect one level up, so every registered name
must appear at an `assert_invariant(` call site in non-test code, and that is enforced.

### What the first version of it cost

Two mistakes worth keeping, both caught by measuring rather than by reading:

- The function was called `require`, and that name **retired `relay/selfimprove/autonomy.py::require` from the inventory** — see the blind-spot table above. Renaming it to `assert_invariant` (which is what the source had called it) both fixed the collision and matched the design.
- `python -m relay.invariants list` printed *"no invariants registered"* with two registered: running the file as `__main__` loads a **second copy** of the module, with its own registry, and the copy doing the printing is the empty one. The entry point calls the package's `main` now.

## Wired, and still not answering the question it was built for

`relay/mechanism_telemetry.py` says in its own header what the whole file is for:

> This file exists to make the graded comparison POSSIBLE by carrying the join key —
> self_report_outcome and patch_hash on every turn — so that joining to the grader is a join
> rather than a reconstruction.

Measured 2026-09-13 on `.fleet/mechanisms.jsonl`:

```
records 5614   with self_report_outcome 2265   with artifact_hash 0
```

Giving `patch_hash` a caller fixes the duplicate implementation. It does **not** fix this.
`artifact_hash` is never passed at any `record()` site, and the reason is structural rather than
an oversight: the telemetry rows are written by the worker as it settles, and the patch is
captured afterwards, out of process, by `bench/pro_capture.py` reading the container through the
routing broker. At the moment the row is written the artefact does not exist yet; at the moment
the artefact exists the row is already on disk and the worker is gone.

So the two sides currently share `instance_id` and a timestamp, which is the "reconstruction"
the header set out to avoid. Closing it needs a decision this burn-down is not the place for:
either the capture writes its own telemetry row keyed to the worker's `run_id` (which means
`pro_capture` learning the run it is capturing), or the worker records a tree digest as its
artefact identity and the grader join is redefined on that instead of on the patch. Both change
what the join means, so neither is a repair.

**Recorded as measured and open.** Not carried as an unreached entry, because the function now
has a caller and the inventory would stop being about callers.

## Triage entries that did not survive verification

The table above is evidence, not a verdict, and two rows have already failed when checked. Both
are recorded here rather than deleted, because "a survey said so" is exactly the kind of claim
that gets re-asserted a year later.

**`scripts/collect_lens_corpus.py::all_inconclusive` — the bug it describes is already fixed.**
The survey reported that the discard gate at `:477` still throws away a panel where every lens
answered INCONCLUSIVE. Measured:

```
INCONCLUSIVE in A.VERDICTS        : True
all_unclear(all-INCONCLUSIVE)     : False    <- True would mean it is discarded
```

`INCONCLUSIVE` is a full member of the vocabulary and is preserved at `:281`
(`verdict if verdict in A.VERDICTS else A.UNCLEAR`), so `all_unclear` is False and the row is
kept. The rounding the docstring describes is history. `all_inconclusive` is an unused helper
whose purpose was served another way — a deletion candidate, not a live defect.

**`scripts/stale_server_check.py::fleet_is_running` — it is NOT the rule the caller applies.**
The survey reported that `_run_appears_live` duplicates it inline. It does not:

```
marker present, pid unreadable
  _run_appears_live            -> True    (conservative: withhold the swap)
  fleet_is_running([(True, False)]) -> False
```

`fleet_is_running` is a pure `any(present and pid_alive)`; `_run_appears_live` adds conservative
branches for an unusable pid and for `status.running`. Substituting one for the other would
drop the safe-side branch and permit a swap while a run might still be live. Wiring it would be
a regression; the open question is whether the pure helper should exist at all.

**The pattern:** a survey that reads a docstring describing a past defect can report that defect
as present. Both of these came from docstrings written in the past tense. Check the behaviour,
not the prose, before acting on any row above.
