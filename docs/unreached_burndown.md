# Burning down the unreached baseline

Started 2026-09-13. **93 → 82** so far. This file exists so the remaining 86 do not have to be
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

The ratchet noticed `is_resolved` on its own: the moment it gained callers the test refused to
keep it listed. That is the mechanism working in the direction it was built for.

## The remaining 82

Classified 2026-09-13 by three parallel surveys, each required to give grep-level evidence and
to answer "could not determine" rather than guess. **These verdicts are triage, not proof** —
the ones acted on so far were re-verified by hand first, and the rest should be too.

### The heaviest UNWIRED — a call site exists and is missing

| function | what is broken today |
|---|---|
| `relay/selfimprove/runtime_config.py::revert_active` | `write_active` **is** wired (`controller.py:342`, fires when a verdict may_activate). Its counterpart, `reset_to_base` and `pending_swap` have zero callers and the module has no CLI: **if an activated genome turns out bad there is no wired path back.** |
| `relay/fleet_retention.py::open_maybe_gz` | the same "open `.jsonl` or `.jsonl.gz`" fix was independently rewritten in `selfimprove/quality.py` and `scripts/win/capture_budget.py`; a third unpatched reader is likely |
| `relay/execution_profiles.py::validate_runtime` | `local_job_store.create_job` resolves a profile and never validates it — a job can be LOCAL_LOOP with no local MCP, and the `RoutingError` never fires |
| `tools/lock_state.py::token_gap` | the writer is wired (`security.py:488`); nothing reads it, so the go/no-go for `MCP_REQUIRE_UNLOCK_TOKEN` is decided blind |
| `relay/mechanism_telemetry.py::patch_hash` | `artifact_hash` is `None` at every real `record()` site, so joining telemetry to the grader stays a reconstruction |
| `relay/profile_token.py::forget_memo` | `reset_socket_route` claims "nothing is preserved" and does not clear this cache, so a token can outlive the browser it came from |

### Deliberately unwired — do not "fix"

`relay/selfimprove/autonomy.py::raise_to` — its docstring says so, and
`test_autonomy.py::test_raise_to_has_no_production_caller` **fails the build if a caller
appears**. A single-machine agent raising its own privilege would be a stub that looks like a
control. Leave it.

### The self-improvement subsystem has no driver

21 of the 86 are in `relay/selfimprove/`. `scripts/run_nightly_real.py` — the script meant to
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
