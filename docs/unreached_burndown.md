# Burning down the unreached baseline

Started 2026-09-13. **93 → 69** so far. The scan and the baseline now agree at 69: the two names the
scanner had never printed at all are in the frozen list with their reason, so a gap between the two
numbers is once again a signal rather than a known discrepancy. This file exists so they do not have
to be triaged a third time.

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

That fix printed two names on its first run that had **never been printed at all**, both
called `compact`: `bridge/session_store.py::compact` (a VACUUM the store's own test is the
only caller of) and `relay/selfimprove/episode_record.py::compact` ("the summary row a
comparison needs", in the subsystem that has no driver). They share a name, so the scan had
been dropping both since the day the second one was written. They are in the inventory under
the `revealed` reason — not new code, but names the scanner could not see at the freeze.

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
| `relay/review_resilience.py::diagnose_after_fresh_replay` | **fixed** — two of its four answers were typed out as literals at two settle paths (`session_state`/`recovered` and `task_content`/`needs_decomposition`, which ARE the enum's values), leaving the other two unreachable: a fresh replay killed by a transient error, and one whose evidence identifies nothing. Those are exactly the runs where `recovery_cause` stayed EMPTY. All four are reachable now and the diagnosis's sentence is recorded; no string a reader already sees changed |
| `relay/quota_meter.py::sustainable_workers` | **fixed** — the meter had 4,698 turn rows and nothing computed the number they are for. Its input never existed either: `per_worker_rpm` had no default and `snapshot()` produced no such field, so it returned 0.0 for any snapshot. The denominator was on disk all along (`record_turn` writes `worker`); measured on the busiest minute, **66 turns / 56 workers → 59.4 sustainable** against a hand-chosen concurrency |
| `bridge/session_store.py::latest_attached` | **deleted** — not unwired, *retired*. It had been deliberately replaced by `latest_session()` on 2026-08-28 after it reached past the newest session into one 54 hours old, and the replacement's comment says so. Keeping the superseded reader listed as "a function with no caller" invited someone to wire the bug back in. 23 lines gone, and its test with it |
| `relay/profile_token.py::discard_template` | **fixed** — `load_template` rejected a template older than the age cap and then *left it on disk*, so the same expired template was re-read and re-rejected on every capture, forever. Measured on the live store: one of the two cached templates was **150.6h old against a 24h cap**. The rejection now discards it |
| `tools/lock_state.py::locked_recently` / `locked_since` / `matching_record` | **fixed** — all three docstrings said they were "kept for the CLI", and `_cli` had `show` and `token-gap` and called none of them. A justification resting on a surface nobody built is worse than none, because it reads as settled. `recent [seconds]` is that surface. **Not `matching_records`** (plural), which is live in three modules and answers a different question — "which refusals could have been mine", a decision — where these answer "what happened" |
| `relay/transport_policy.py::evolvable_fields` | **fixed** — it declared `transport_explore_rate`, and a scan over `git ls-files` found that name in exactly one place: that return tuple. Nothing read it — the fifth instance of the defect the version table twelve lines above says this repository "has now found in four separate components", sitting inside the fix for the first four. The remaining knob is spelled once and read through its name, and `choose()` now records a genome knob no policy reads, because an undeclared knob is an A/B arm identical to its control |

The ratchet noticed `is_resolved` on its own: the moment it gained callers the test refused to
keep it listed. That is the mechanism working in the direction it was built for. It did the same
for `evolvable_fields` on 2026-09-14, refusing to keep it listed within seconds of the wiring.

## The remaining 69

Classified 2026-09-13 by three parallel surveys, each required to give grep-level evidence and
to answer "could not determine" rather than guess. **These verdicts are triage, not proof** —
the ones acted on so far were re-verified by hand first, and the rest should be too.

### The heaviest UNWIRED — a call site exists and is missing

**Empty, for the first time.** Both entries were wired on 2026-09-13 — `lock_state::token_gap`
and `mechanism_telemetry::patch_hash` — and they are listed under *What left the baseline*
above. A name belongs here when a caller for it exists somewhere in the design and is simply
missing; that is the shape worth doing next, so the heading stays.

### Triaged to a verdict: the refusal taxonomy

`relay/review_resilience.py` defines four refusal detectors and one diagnosis. `relay_fleet`
imports **one** of the detectors. Measured rather than assumed, by running all four against the
reply that actually ended run `r6aa597a8`: **all four False**. The reply is nevertheless
recognised — 「それに応答できませんでした」 is the FIRST entry of
`relay_fleet.CANNED_NONANSWER_MARKERS`, which has its own live branch. Reading one module and
concluding "nothing matched" was wrong, and is recorded that way in the failure-mode registry.

| entry | verdict |
|---|---|
| `looks_like_transient_error` | **DUPLICATE.** `relay_fleet.TRANSIENT_ERROR_MARKERS` is live and a superset (予期しないエラー / システムエラー / unexpected error / something went wrong, plus reload-the-page and if-the-problem-persists). Deleting it is the right answer; it is left listed until someone confirms nothing outside this repository imports it |
| `looks_like_capability_failure` | overlaps `TOOL_UNREACHABLE_MARKERS` in intent, not in strings. Neither is a superset. **Could not determine** whether the difference is meaningful without a corpus of capability-failure replies, and there is no such corpus |
| `looks_like_output_filter` | no live equivalent found. Its four markers (出力できませんでした / 応答を生成できませんでした / response was filtered / content filter blocked) would be a real fifth family, and adding a marker family to a live path needs the measurement the canned-non-answer family got before IT was added: "6 occurrences in 6,585 assistant replies on record, every one of them 89 to 103 characters long". That measurement has not been taken |
| `diagnose_after_fresh_replay` | **wired** — see above |

The rule those three are waiting on is the one already written into
`CANNED_NONANSWER_MARKERS`' own comment: *a marker that fires on a real answer costs more than
one that misses.* None of them may be wired on the strength of reading their strings.

### Triaged by hand, 2026-09-14: four more clusters

Following the procedure below rather than the survey's verdicts. Each one was checked by asking
which of the module's functions production actually calls, not by reading the docstrings.

**`relay/quota_meter.py::sustainable_workers` — WIRED.** The meter had written 4,698 turn rows
since 2026-09-01 and the one function that turns them into a decision had no caller. Not a
forgotten call: `per_worker_rpm` had no default and `snapshot()` produced no such field, so it
returned `0.0` for any snapshot — the shape `execution_profiles.validate_runtime` has, where the
INPUT does not exist. The denominator was on disk the whole time (`record_turn` writes `worker`
on every row), so `snapshot()` now counts distinct workers per minute and the function defaults
from it. Measured on the busiest minute on record: **66 turns / 56 workers → 1.18 per worker →
59.4 sustainable**, against a fleet whose concurrency was a number typed on a command line.
**Not wired into admission**, deliberately — capping concurrency is a policy change to a live
control, and this session already produced one threshold change that measurement took back.

A window bug fell out of measuring it: `rpm` counted `ts >= now - 60` with no upper bound, so
`snapshot(now=<a past time>)` counted every later row — 4,698 of them. Harmless at
`now=time.time()`, wrong for the only reason that parameter exists.

**`relay/turn_outcome.py::is_capacity_signal` — the other half of the same gap.** `classify` is
live in 15 files; this predicate ("should a controller reduce concurrency") is called nowhere,
and **nothing in the fleet reduces concurrency on a rate class** — checked across every
non-test file. The system has 147 recorded `rate` refusals and reached 93% of the hourly quota.
The predicate is one line; what is missing is a controller to consult it, which is the same
decision as enforcing `sustainable_workers`. Left listed with that stated.

**`relay/provenance.py::adjudicate` / `outranks` / `resolved_value` — no consumer anywhere,
and none of the obvious candidates is right.** Of the module's ten functions production calls
exactly two: `normalise` (4 sites) and `require_authority_for_evolution` (3) — so the
lineage-poisoning gate itself IS wired. The adjudicator is a general facility for competing
claims about one fact at different authorities, and the two places that look like consumers are
not: `RelayWorker._claim_verdict` asks a narrower, binary question and already delegates to
`evidence_manifest.assess`, and `fleet_reconcile` compares claims that all carry the SAME
authority, which `adjudicate` refuses to resolve by design. **What would justify wiring it is a
second source of evidence at a different authority about the same subject — which the system
does not currently produce.** Recorded rather than forced.

**`tools/tool_probe.py::classify_probe_reply` / `next_probe_instruction` — a half-wired
protocol.** ~~`verify_probe_reply` is live in 3 files~~ — **wrong, corrected 2026-09-14 below.**
The classifier and the next-instruction generator are not called. So the bridge verifies a probe
reply but never issues a follow-up probe or classifies what came back. Whether the protocol was
meant to have more than one round is the question, and it is a design question rather than a
missing call.

### Triaged by hand, 2026-09-14 (second pass): a retirement and two half-built surfaces

**`bridge/session_store.py::latest_attached` — DELETED, and the distinction matters.** Its own
history says why: on 2026-08-28 `latest_session()` was written to replace it because
`latest_attached` sorted by attach time and handed back a session **54 hours old** while a newer one
existed. The replacement is live; the original stayed in the tree with a docstring that still reads
like an offer. A superseded implementation sitting in the unreached list is a trap, because the list
is a to-do list of things to *wire* — the correct action here was the opposite one. 23 lines and a
17-line test removed, with a note left on `latest_session()` saying why it must not come back.

**`relay/profile_token.py::discard_template` — WIRED, and it was hiding a live leak.** `load_template`
compared a cached template's age against the cap and returned `None` when it was over — without
removing it. So an expired template is re-read from disk, re-parsed and re-rejected on **every single
capture**, permanently, and the eviction function written for exactly that moment had no caller.
Measured on the live store before the change: two cached templates, one of them **150.6 hours old
against a 24-hour cap**. The age-cap branch now calls `discard_template` before returning.

**`tools/lock_state.py::locked_recently` / `locked_since` / `matching_record` — WIRED, all three.**
Each says in its own docstring that it is kept for the CLI ("kept for the CLI and for diagnostics,
where naming the last refusal is exactly the question"), and `_cli` offered `show` and `token-gap`
and called none of them. That is a worse state than an unjustified name: it reads as decided, so
nobody asks. `recent [seconds]` is the diagnostic they were kept for — was anything refused in the
last N seconds, when, and which record.

**Not `matching_records`** (plural). That one is live in three modules and answers a different
question — *which refusals could have been mine*, which a caller uses to decide — where these three
answer *what happened*, which is what a person looking at a stuck worker asks.

The first draft of the reader printed what looked like a contradiction: over a 24-hour window,
`locked_recently: true`, `locked_since: false`, `record: {}`. That is not a defect in the three
functions — `locked_since` and `matching_record` are **additionally** capped at `DEFAULT_FRESH_SEC`
so a clock jump cannot resurrect an ancient refusal, while `locked_recently` honours the window it
is given. Two windows, two questions. The defect was in the presentation, and the fields are named
for their windows now (`asked_window_s` / `fresh_window_s`, with the record's age printed beside
them) — pinned by a test that asserts the disagreement is legible rather than asserting it away.

### The pattern under half the list: a decision surface with no decider

Counted 2026-09-14 over the 69. These are not scattered leftovers. **34 of them belong to six
subsystems that were each built complete — logic, documentation, tests — and are consulted by
nothing.** They are unreached because their *callers* were never written, so triaging them one at
a time cannot terminate: every answer is "the caller does not exist", and the decision that would
create one is above the level of this burn-down.

| subsystem | listed | what has no caller | what is actually missing |
|---|---:|---|---|
| `relay/selfimprove/` | 24 | the whole loop | a driver. `scripts/run_nightly_real.py` opens *"It has never been run at all."* No CI job, scheduler, `.bat` or cron invokes any entry point |
| `relay/fleet_toolset.py` | 1 (3 real) | `check`, `mode`, `unknown_tools` | a consumer. `main.py` removed the call site deliberately and left the list "for the runner that owns it"; the runner does not consult it either |
| `tools/tool_probe.py` | 3 | `verify_probe_reply`, `classify_probe_reply`, `next_probe_instruction` | two of the three are RETIRED, not unbuilt — see the correction below |
| `relay/provenance.py` | 3 | `adjudicate`, `outranks`, `resolved_value` | a second source of evidence at a different authority about the same subject, which the system does not produce |
| `relay/autonomy_gate.py` | 2 | `judge_autonomy`, `constraints_text` | a run-level gate. Its STOP/ASK **vocabulary** is live in `task_router._static_risk` and `contract_gate`; its **verdict** is not, because the fleet gates per job and this judges a whole run |
| `relay/turn_outcome.py` | 1 | `is_capacity_signal` | a controller. Nothing in the fleet reduces concurrency on a rate class — checked across every non-test file |

**Why this is one row and not six.** The burn-down's procedure — take an entry, find the caller
that should exist, write it — works on the other 35 and cannot work on these. What they need is a
decision per *subsystem*: run it, wire it, or retire it. Until that is made, every pass over the
list re-derives the same six answers, which is what the first two surveys did.

**`relay/fleet_toolset.py` is the one to read first**, because it shows what the state costs. Its
test section was headed *"enforcement: shadow by default, and the gateway actually consults it"*
and not one test checked the second half — `main.py` had removed the call site. The guard the
module calls "the point of the whole module" read a dump under gitignored `.fleet/`, so it
**skipped in CI on every run since it was written**, and on the one machine with a dump it
compared against a snapshot taken by hand on 2026-08-30 that nothing refreshes. Pointed at the
real registry it found **eleven tools nobody had decided about**, among them `fleet_submit` (a
worker enqueuing fleet runs), `roll_back` (returning the very tree the capture step grades) and
the `recurrent_*` loop drivers. All eleven are recorded now, and the guard reads the registry.

**And the registry has to be read from a clean interpreter.** The first version imported `main`
inside the pytest session and got a registry the session had already altered: conftest's autouse
`_no_desktop_toasts` replaces `notify_ops.notify_desktop` with a local function named `_capture`,
and `_ALL_TOOLS` is keyed by `__name__` — so the in-process import reported `notify_desktop`
missing, `_capture` present, and six turn tools absent. conftest names this hazard forty lines
above that fixture: *"the failure looks like a bug in the tool map and is a bug in what the test
inherited."* It is read in a subprocess now.

**The scanner could not have raised any of it.** `check` and `mode` are each defined in several
modules, and `tools/unreached.py` drops a name it sees defined twice — the fourth blind spot,
found 2026-09-13. Only `unknown_tools`, a name unique to that file, ever reached the baseline.
The list understates this row: the dead gate was behind the blind spot, not on the list.

#### Correction: `verify_probe_reply` is not live

An earlier entry above said *"`verify_probe_reply` is live in 3 files; the classifier and the
next-instruction generator are not."* That was a substring grep over text, and every one of those
three mentions is a comment naming the function. An AST scan over `git ls-files` for real call
sites finds **zero** for all three.

What actually happened is better than a gap: `verify_probe_arrival` replaced it, because the old
one asked the agent to transcribe a secret back and *the live agent refused*.
`tools/test_tool_probe_inbound.py` pins that both probe sites call the new one and neither calls
the old — using an AST scan, for the same reason the grep was wrong, and its docstring says so:
*"Parse, don't grep."* So the disposition is:

- `verify_probe_reply` — **retired**, kept deliberately: `verify_probe_arrival`'s contract is
  defined as mirroring it ("same `kind` vocabulary, same precedence"), and a test cross-checks
  the two. Deleting it would orphan the definition the live function is written against.
- `classify_probe_reply` — the vocabulary anchor that equivalence is asserted against.
- `next_probe_instruction` — the only genuinely unbuilt one: a second probe round nothing asks
  for. Whether the protocol was meant to have more than one round is a design question.

#### Also recorded, not resolved: two declarations of what may evolve

`transport_policy.evolvable_fields()` names the knobs a genome may move; `selfimprove/manifest.py`
holds `PARAMETER_TYPES`, the registry the evolution campaign actually sweeps. Neither transport
knob is in it, so `campaign.variants_for` raises *"not an evolvable coordinate"* for either one
and `coordinates()` never sweeps them. They cannot simply be added: `PARAMETER_TYPES` entries are
numeric `(low, high)` ranges and `transport_eligible_kinds` is a list of kinds.

That type gap has a visible consequence, asserted rather than patched: `_policy_v2` does
`kind not in set(eligible)`, so a knob of the wrong type raises `TypeError` out of a transport
decision — against this repository's own rule that a policy which can crash its caller is worse
than no policy. It is unreachable today (the one production call site passes no knobs, and the
genome path that would has no driver), and wrapping the comparison would hide the disagreement
instead of settling it. Settling it means deciding whether transport knobs belong in the
manifest, which is the same subsystem decision as the table above.

### Deliberately unwired — do not "fix"

`relay/selfimprove/autonomy.py::raise_to` — its docstring says so, and
`test_autonomy.py::test_raise_to_has_no_production_caller` **fails the build if a caller
appears**. A single-machine agent raising its own privilege would be a stub that looks like a
control. Leave it.

### The self-improvement subsystem has no driver

24 of the 69 are in `relay/selfimprove/` — the share has GROWN as the rest came down (24/75 → 24/69;
not one of them has moved), and one of the two names the scanner had never printed is in there too.
`scripts/run_nightly_real.py` — the script meant to
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

### The item the analysis skipped, and what it turned out to be worth

The run that produced that analysis was refuted twice, and **the second refutation was right**:
the goal named `scripts/coverage-partitions.ts` (28,662 bytes, real) as a primary target, and the
report mentions it **zero times** — neither evaluated nor listed under 「見たが該当なし」, which
the goal explicitly required. It reported `coverage-uncovered-locations.cjs` instead.

**The goal misdescribed the file it named.** Its parenthetical — 「per-fileカバレッジで未到達
コードを炙り出す仕組み」 — describes the *reporter*, which is `coverage-uncovered-locations.cjs`.
The agent followed the description; the refuter followed the name. Both are defensible, and the
answer is that they are two different mechanisms and the report owed an entry for each.

`coverage-partitions.ts` is the *runner*, not the reporter. Two ideas in it:

| | |
|---|---|
| **Longest-processing-time partitioning weighted by MEASURED durations** | per-file times are persisted between runs, refreshed from each run's reporter output, unknown files get a default weight, and placement is a min-heap over buckets (`assignWeightedPartitions`). Heaviest first into the lightest bucket. |
| **A partition must never judge** | `DSH_COVERAGE_PARTITION_MODE=1` suppresses reports *and thresholds* inside each partition; only the merge runs them. A partial view never produces a verdict. |

The coverage machinery itself does not transfer — we have no coverage thresholds, and the
unreached ratchet is AST-based. **The partitioning does**, and it is worth it on measurement
rather than on principle. `pytest --durations=0` over the preflight file list, 2026-09-13:

```
9,808 timed entries   1,441 s total
top 20 entries        424 s   (29%)
top 50 entries        613 s   (43%)
heaviest single file  relay/test_edge_auth.py  64.3 s
```

Concentrated enough that naive splitting strands one worker with the tail, and flat enough that
balancing works: the 64 s file is the floor, so four LPT-weighted partitions put the gate near
~360 s against 1,441 s serial. That gate ran **fifteen times** in one session.

The second idea has no code to write and is worth stating anyway, because this repository has
the same hazard in `preflight --quick`, whose own comment already worries about it: *"a signal
that is never clean stops being read, and then the one that matters (--quick dropping the pytest
gate) is lost in it."* **A partial run must not be allowed to produce a verdict** is the rule
`DSH_COVERAGE_PARTITION_MODE` enforces mechanically, and `--quick` enforces by printing a
warning.

NOT BUILT HERE. Parallelising the pytest gate changes CI as well as preflight, and the entry
above is the evaluation the report owed, not the implementation. The measurement is recorded so
the decision is about a number rather than about an intuition.

### The third item, which is not about the baseline at all

The same analysis listed a `change-scope` reporter last and low, and said plainly that it is not
a remedy for baseline rot. What it supports is the rule this repository enforces by hand: never
`git add -A`; name the files the work changed. `scripts/change_scope.py` prints the four states
separately — committed-but-unpushed, staged, unstaged, untracked — and its `--add-line` names
only the tracked ones.

That is not tidiness. Run on this working tree it reports **nine** untracked paths that are
neither ignored nor part of the project, and `check_no_identifying_names.py` reports 86 of their
files as carrying identifying content, with the sentence that is the whole argument: *"They are
not public, and they are one `git add` from being so."* The git call is pinned deterministic —
fsmonitor off, `GIT_OPTIONAL_LOCKS=0`, `LANG=C` — because a stale index or a localised status
word each give a wrong answer that looks like a right one.

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
