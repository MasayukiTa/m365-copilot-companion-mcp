# -*- coding: utf-8 -*-
"""Nothing new gets built without a caller.

THE REPOSITORY'S SIGNATURE DEFECT, and every instance of it so far cost its own investigation:
looped-transformer step 0 (implemented, registered, 0 calls); the four fan-in functions; the
147 lines of relay/turn_outcome.py; supervisor_verify, reachable only from a shadow bench path;
contract_gate's `budget_turns`, where the reader existed and the writer had no parameter to set
it, so the branch could not be reached at all; and socket_driver.conversation_ids, whose own
docstring said "NOT PERSISTENCE -- just the ability to be asked" while nothing asked, which is
why a fleet conversation could not be continued for weeks.

I added one myself on 2026-09-12 -- `tools/childproc.run_ok`, unreferenced within the hour of
writing it. It was deleted rather than listed, which is the outcome this test is for.

WHAT THIS ENFORCES. The inventory below was taken on 2026-09-12: 94 module-level public
functions with no reference anywhere in non-test code. It may SHRINK and never grow. A function
that gains a caller must be removed from it, so the list cannot quietly become a historical
document that the next reader takes for the current state.

WHAT THE SCAN CANNOT SEE (tools/unreached.py says this too, and it is why a row is a question
rather than a verdict): a name reached through getattr or a table of handler strings; a name
that shadows a stdlib or library method; class methods, which are excluded entirely. Several
baseline entries are certainly reached through dispatch. They cost nothing sitting here -- the
value is that a NEW one has to be justified.

A ROW IS ANSWERED BY EITHER WIRING IT OR DELETING IT. "Wire it" is not the automatic answer:
`judge_autonomy` (relay/autonomy_gate.py) is a GO/ASK/STOP safety gate with no caller and no
test, and its risk vocabulary was already extracted into task_router._static_risk, which IS
live -- so the question there is which of its three orphaned signals (dirty worktree, no
verification gate, budget over 12 hours) are worth having, not how fast to switch a STOP gate
on. Turning on a gate that refuses work is its own decision, and this repository has already
declined one (the judge enforce item) for reasons that have not changed.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import unreached as U  # noqa: E402


#: Taken 2026-09-12. "<path>::<name>" -- a bare name collides across modules and a line number
#: moves on every edit above it.
#:
#: THESE HAVE NO TEST EITHER, which makes them the starkest: nothing calls them and nothing
#: checks them.
NO_CALLER_NO_TEST = {
    # Forged by the tool foundry, registered by a directory walk. See REASONS below.
    "tools/auto/office_password_recovery.py::office_password_recovery",
    "relay/autonomy_gate.py::judge_autonomy",                    # 54 lines
    "tools/judge_backend.py::ask_human_async",                   # 18 lines
    "relay/autonomy_gate.py::constraints_text",                  # 5 lines
    "relay/review_resilience.py::looks_like_capability_failure", # 2 lines
    "relay/review_resilience.py::looks_like_output_filter",      # 2 lines
}

#: Called by nothing outside tests. Tests referencing a function say it was worth writing; they
#: do not say anything reaches it in production.
NO_CALLER_BUT_TESTED = {
    # MOVED OUT OF NO_CALLER_NO_TEST 2026-09-19, AFTER THE INSTRUMENT WAS FIXED. That set says
    # "nothing calls them and nothing checks them" and for these four the second half was
    # false. `tests/` was not in tools/unreached.ROOTS, so no file under it was read for any
    # purpose and the "refs in tests" column answered 0 for everything the tests/ suite covers:
    # run_trajectory reported 0 against the five references in tests/test_golden.py, a file
    # that exists for nothing else. capture_fn's OWN DOCSTRING names the two tests in
    # tests/test_lean_capture.py that assert through it -- the prose knew and the inventory
    # said the opposite. The two `compact`s needed no fix to the scan at all; they were simply
    # filed in the wrong set by hand, which is what test_the_two_sets_agree_with_the_scan now
    # stops.
    "tools/golden.py::run_trajectory",                              # 64 lines, 4 test refs
    "bench/skill_use_log.py::compare_runs",                         # 24 lines, 4 test refs
    "relay/lean_capture.py::capture_fn",                            # 10 lines, 7 test refs
    "relay/selfimprove/episode_record.py::compact",                 # 6 test refs
    # DEAD SUBGRAPHS, revealed 2026-09-14 by iterating the scan to a fixed point. Each was held
    # off the list by a caller that is itself unreached, so a reference count said "someone
    # names this" while nothing could get there. Not new code.
    "relay/selfimprove/diversify.py::diversify",                    # 62 lines
    "relay/solve_policy.py::plan_solve",                            # 56 lines
    "relay/selfimprove/calibration.py::recommend_effort",           # 33 lines
    "relay/selfimprove/propose.py::mutation_generator",             # 33 lines
    "relay/selfimprove/guards.py::classify_outcome",                # 21 lines
    "bench/companionbench/shadow_rules.py::verdict",                # 20 lines
    "tools/env_portability.py::parse_env",                          # 14 lines
    "relay/project_memory.py::entry_authority",                     # 11 lines
    "bench/companionbench/shadow_rules.py::old_verdict",            # 9 lines
    "bench/companionbench/shadow_rules.py::new_verdict",            # 6 lines
    "relay/selfimprove/calibration.py::competence",                 # 5 lines
    "relay/lean_capture.py::enabled",                               # 3 lines
    "tools/env_portability.py::classify",                           # 3 lines

    "bench/skill_probe.py::compare",                               # 51 lines, revealed 2026-09-14
    "bench/companionbench/shadow_rules.py::compare",               # 41 lines, revealed 2026-09-14
    "relay/selfimprove/planner_evaluator.py::preflight",           # 39 lines, revealed 2026-09-14
    "relay/outcomes.py::tally",                                    # 35 lines, revealed 2026-09-14
    "relay/selfimprove/decision.py::summarise",                    # 21 lines, revealed 2026-09-14
    "relay/turn_outcome.py::summarise",                            # 13 lines, revealed 2026-09-14
    "relay/selfimprove/harness_feedback.py::report",               # 11 lines, revealed 2026-09-14
    "relay/selfimprove/coreset.py::summarise",                     # 8 lines, revealed 2026-09-14
    "relay/provenance.py::adjudicate",                           # 82 lines
    "relay/selfimprove/propose.py::propose_candidates",          # 68 lines
    "relay/selfimprove/routing.py::held_out_advantage",          # 65 lines
    "bench/companionbench/baseline.py::why_they_flip",           # 54 lines
    "relay/selfimprove/autonomy.py::raise_to",                   # 48 lines
    "relay/selfimprove/apply.py::safe_commit",                   # 39 lines
    "bench/companionbench/baseline.py::repeat_suite",            # 36 lines
    "tools/env_portability.py::merge_for_new_machine",           # 35 lines
    "bench/attempt_snapshots.py::transitions",                   # 32 lines
    "relay/selfimprove/solver_feedback.py::to_hypotheses",       # 31 lines
    "relay/selfimprove/autonomy.py::require",                    # 29 lines
    "tools/coding_ops.py::worktree_scope",                       # 29 lines
    "tools/tool_probe.py::verify_probe_reply",                   # 27 lines
    "bridge/session_store.py::search_turns",                     # 26 lines
    "tools/tool_probe.py::classify_probe_reply",                 # 26 lines
    "relay/selfimprove/trace_to_eval.py::record_correction",     # 25 lines
    "tools/judge_backend.py::sampling_judge_async",              # 24 lines
    "scripts/stale_server_check.py::decide_post_update_action",  # 20 lines
    "relay/selfimprove/harness_tree.py::justified",              # 17 lines
    "bench/companionbench/runner.py::solver_feedback_entries",   # 15 lines
    "relay/project_memory.py::list_themes",                      # 15 lines
    "tools/tool_probe.py::next_probe_instruction",               # 15 lines
    "relay/selfimprove/autonomy.py::lower_to",                   # 14 lines
    "relay/selfimprove/harness_tree.py::branches",               # 14 lines
    "relay/turn_outcome.py::classify_turns",                     # 13 lines
    "relay/provenance.py::outranks",                             # 12 lines
    "relay/provenance.py::resolved_value",                       # 12 lines
    "relay/execution_profiles.py::validate_runtime",             # 11 lines
    "relay/selfimprove/guards.py::partition_outcomes",           # 11 lines
    "relay/fleet_toolset.py::unknown_tools",                     # 10 lines
    "relay/turn_outcome.py::is_capacity_signal",                 # 10 lines
    "scripts/stale_server_check.py::fleet_is_running",           # 10 lines
    "relay/solve_policy.py::finalize",                           # 9 lines
    "relay/solve_policy.py::plan_and_explain",                   # 9 lines
    "relay/project_memory.py::authorities_in",                   # 8 lines
    "relay/chathub.py::collect_text",                            # 7 lines
    "relay/relay_fleet.py::connector_proven",                    # 3 lines
    "relay/selfimprove/compare.py::transport_versions_differ",   # 3 lines
    "relay/selfimprove/guards.py::is_domain_general",            # 3 lines
    "tools/security.py::clear_presented_token",                  # 2 lines
}

BASELINE = NO_CALLER_NO_TEST | NO_CALLER_BUT_TESTED


#: The three answers that let a name stay unwired. A CLOSED SET, the same shape
#: relay/outcomes.py uses and for the same reason: an open field is filled with whatever the
#: writer was thinking, and a reader then cannot tell a triaged entry from a tired one.
#:
#:   dispatch    reached by name rather than by reference -- getattr, a table of handler
#:               strings, a decorator registry. The pointer names the file that does the
#:               dispatching.
#:   entrypoint  invoked from outside Python: a .ps1, a scheduled task, a console shortcut.
#:               The pointer names that caller.
#:   deliberate  kept, unwired, on purpose. The pointer names where the decision is written
#:               down, because "we decided" with no location is how judge_autonomy came to sit
#:               here for weeks with nobody able to say who had decided what.
#:
#: THE POINTER IS CHECKED, THE PROSE IS NOT. A word count would be satisfied by a word count;
#: a path either exists in the repository or it does not.
#:   revealed    the scanner could not SEE it at the freeze. Not new code: a name
#:               defined in two modules used to be dropped from the scan entirely, so a
#:               finding could be retired by an unrelated function taking its name. The
#:               pointer names where the blind spot is written up. Such an entry still
#:               needs the same triage as the pre-freeze ones -- it is a reason it is on
#:               the list, not a reason it may stay unwired forever.
ALLOWED_REASONS = ("dispatch", "entrypoint", "deliberate", "revealed")

#: "<path>::<name>": ("<reason>", "<path that proves it>")
#:
#: EMPTY ON PURPOSE. Nothing has been added to the inventory since the freeze, and filling this
#: in for the 77 entries that predate it would mean writing 77 sentences from inference -- the
#: exact move that put `all_inconclusive` and `fleet_is_running` on a triage list they did not
#: survive. See docs/unreached_burndown.md.
REASONS: dict[str, tuple[str, str]] = {
    # A FORGED TOOL HAS NO STATIC CALLER, BY CONSTRUCTION. tools/auto/ is where the tool
    # foundry writes forged modules, and main.py registers them by WALKING that directory
    # (tools/auto_loader.py::load_auto_tools, main.py ~962) -- a reference no count can see.
    # This is a property of the directory, so every future forged tool arrives here the same
    # way and is a real entry rather than a scanner bug.
    "tools/auto/office_password_recovery.py::office_password_recovery":
        ("dispatch", "docs/unreached_burndown.md"),
    # A REFERENCE COUNT IS NOT A REACHABILITY ANALYSIS, closed 2026-09-14 by iterating the scan
    # to a fixed point (5 rounds, 80 -> 96). Every one of these sixteen was held off the list by
    # a caller that is itself unreached: something named it, and nothing could get there. Three
    # had already been found by hand while chasing other things, which is what prompted the
    # loop. Not new code -- dead subgraphs behind leaves that were already on the list.
    #
    # RECLASSIFIED 2026-09-24, C-1 CLOSE-OUT (scratchpad/c1_final_verdicts.md, aggregated into
    # docs/unreached_burndown.md's "C-1 closed out" section). "revealed" only ever said HOW a
    # row arrived (its only caller was itself unreached); it is not one of ALLOWED_REASONS'
    # closing kinds. Every row below now has an actual verdict, so the kind says what was
    # decided. `worktree_remove` and `worktree_add` left the inventory the same day, WIRED.
    # `tools/env_portability.py::parse_env` / `::classify` are PENDING new-PC work and stay
    # "revealed" on purpose -- nobody has triaged them yet.
    "relay/selfimprove/diversify.py::diversify": ("deliberate", "docs/unreached_burndown.md"),
    "relay/solve_policy.py::plan_solve": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/calibration.py::recommend_effort":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/propose.py::mutation_generator":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/guards.py::classify_outcome":
        ("deliberate", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::verdict":
        ("deliberate", "docs/unreached_burndown.md"),
    "tools/env_portability.py::parse_env": ("revealed", "docs/unreached_burndown.md"),
    "relay/project_memory.py::entry_authority": ("deliberate", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::old_verdict":
        ("deliberate", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::new_verdict":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/calibration.py::competence":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/lean_capture.py::enabled": ("deliberate", "docs/unreached_burndown.md"),
    "tools/env_portability.py::classify": ("revealed", "docs/unreached_burndown.md"),

    # `episode_record.py::compact` was always unreached and never printed at all: `compact` is
    # defined in two modules, and a colliding name used to be skipped by the scan rather than
    # reported. Found 2026-09-13 when a new `require` silently retired
    # relay/selfimprove/autonomy.py::require the same way. `bridge/session_store.py::compact`
    # shared that discovery and left the inventory 2026-09-24, WIRED.
    "relay/selfimprove/episode_record.py::compact": ("deliberate",
                                                    "docs/unreached_burndown.md"),

    # THE SAME BLIND SPOT, MEASURED AND CLOSED 2026-09-14. `unreached.py` skipped a name
    # defined in more than one module whenever ANY definition was referenced -- the half of
    # the 2026-09-13 fix that was left. 421 definitions sat behind that skip. Attributing a
    # reference to the module the AST names reports these eighteen; each was checked against
    # the owning module's own importers before being listed. RECLASSIFIED 2026-09-24 for the
    # same reason as the block above -- "revealed" said how they arrived, not what to do about
    # them, and they now have verdicts. `solver_feedback.py::tally` and `apply.py::apply_genome`
    # left the inventory the same day (DELETED and WIRED respectively).
    "bench/skill_probe.py::compare":
        ("deliberate", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::compare":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/planner_evaluator.py::preflight":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/outcomes.py::tally":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/decision.py::summarise":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/turn_outcome.py::summarise":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/harness_feedback.py::report":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/lean_capture.py::capture_fn":
        ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/coreset.py::summarise":
        ("deliberate", "docs/unreached_burndown.md"),

    # KEPT UNWIRED, AND THE DECISION HAS A LOCATION NOW. This table's own comment names
    # judge_autonomy as the cautionary case -- "'we decided' with no location is how
    # judge_autonomy came to sit here for weeks with nobody able to say who had decided
    # what" -- and the decision had in fact been written down since then, at
    # docs/unreached_burndown.md:332: its STOP/ASK VOCABULARY is live, in
    # task_router._static_risk and contract_gate; its VERDICT is not, because the fleet
    # gates per job and this judges a whole run. Two different questions, and only the
    # first one is the fleet's.
    #
    # `constraints_text` renders what `judge_autonomy` decided, so it is unreachable for
    # exactly the same reason and not for one of its own. Listing it without saying so
    # invites someone to wire the renderer and conclude the gate is live.
    #
    # NOT INFERENCE, which is what this table refuses. The pointer is a path that says the
    # thing; the alternative on offer was writing 77 sentences from guesswork, and the two
    # entries that got that treatment (`all_inconclusive`, `fleet_is_running`) did not
    # survive their own triage.
    # NOT WIRED ON PURPOSE, decided 2026-09-19 while fixing the defect it looks like it should
    # have fixed. `diagnose_after_fresh_replay`'s TRANSIENT branch was unreachable because
    # DELETED 2026-09-22, WHICH IS WHY IT IS NO LONGER LISTED HERE. It was carried as
    # "deliberate": every caller passed `fresh_was_transient_error=False` as a literal, and
    # this predicate was written to compute that argument. Calling it would have made a THIRD
    # copy of a judgement relay_fleet already holds in five marker families
    # (transient/agent-dead, tool-unreachable, canned-nonanswer, admin-block, throttle), one
    # of which was already a superset of its six strings. docs/unreached_burndown.md had
    # reached the same verdict independently and called it DUPLICATE.
    #
    # "Deliberate" was the right holding answer and not a resting place: an unwired detector
    # sitting beside four wired ones reads as a family with a gap, and the row itself invites
    # the next person to close it by wiring -- which would re-introduce the third copy this
    # repository had already refused. A row answered by DELETING is the row leaving.
    "relay/autonomy_gate.py::judge_autonomy": ("deliberate", "docs/unreached_burndown.md"),
    "relay/autonomy_gate.py::constraints_text": ("deliberate", "docs/unreached_burndown.md"),

    # C-1 CLOSE-OUT, 2026-09-24. The commander's aggregation of all 84 rows scanned for this
    # burndown (scratchpad/c1_final_verdicts.md) reached a verdict for every remaining row that
    # was not already PENDING (new-PC work) or already decided above. WIRED/DELETED rows left
    # the inventory the same day and are not here; PENDING rows (env_portability's three,
    # stale_server_check's two) get no entry at all, by the legend's own instruction. Every
    # entry below is either D (a standing decision not to wire something with a caller that
    # would exist if wired) or FOLLOWS (its only caller is another row on this list, itself
    # deliberate) -- see docs/unreached_burndown.md's "C-1 closed out" section for the
    # one-line reason behind each name.
    "tools/golden.py::run_trajectory": ("dispatch", "docs/unreached_burndown.md"),

    # relay/selfimprove -- the loop with no driver (scripts/run_nightly_real.py has never run).
    "relay/provenance.py::adjudicate": ("deliberate", "docs/unreached_burndown.md"),
    "relay/provenance.py::outranks": ("deliberate", "docs/unreached_burndown.md"),
    "relay/provenance.py::resolved_value": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/propose.py::propose_candidates": ("deliberate",
                                                        "docs/unreached_burndown.md"),
    "relay/selfimprove/routing.py::held_out_advantage": ("deliberate",
                                                        "docs/unreached_burndown.md"),
    "relay/selfimprove/autonomy.py::raise_to": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/apply.py::safe_commit": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/solver_feedback.py::to_hypotheses": ("deliberate",
                                                            "docs/unreached_burndown.md"),
    "relay/selfimprove/autonomy.py::require": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/trace_to_eval.py::record_correction": ("deliberate",
                                                              "docs/unreached_burndown.md"),
    "relay/selfimprove/harness_tree.py::justified": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/autonomy.py::lower_to": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/harness_tree.py::branches": ("deliberate", "docs/unreached_burndown.md"),
    "relay/selfimprove/guards.py::partition_outcomes": ("deliberate",
                                                        "docs/unreached_burndown.md"),
    "relay/selfimprove/compare.py::transport_versions_differ": ("deliberate",
                                                                "docs/unreached_burndown.md"),
    "relay/selfimprove/guards.py::is_domain_general": ("deliberate", "docs/unreached_burndown.md"),

    # bench -- one-off / manual analysis tools whose output is recorded in results/*, not a
    # production consumer.
    "bench/companionbench/baseline.py::why_they_flip": ("deliberate",
                                                        "docs/unreached_burndown.md"),
    "bench/companionbench/baseline.py::repeat_suite": ("deliberate",
                                                        "docs/unreached_burndown.md"),
    "bench/companionbench/runner.py::solver_feedback_entries": ("deliberate",
                                                                "docs/unreached_burndown.md"),
    "bench/skill_use_log.py::compare_runs": ("deliberate", "docs/unreached_burndown.md"),
    "bench/attempt_snapshots.py::transitions": ("deliberate", "docs/unreached_burndown.md"),

    # tools -- settled docs, test-isolation twins, and a contextmanager reached only in-process.
    "tools/coding_ops.py::worktree_scope": ("deliberate", "docs/unreached_burndown.md"),
    "tools/tool_probe.py::verify_probe_reply": ("deliberate", "docs/unreached_burndown.md"),
    "tools/tool_probe.py::classify_probe_reply": ("deliberate", "docs/unreached_burndown.md"),
    "tools/tool_probe.py::next_probe_instruction": ("deliberate", "docs/unreached_burndown.md"),
    "tools/judge_backend.py::sampling_judge_async": ("deliberate", "docs/unreached_burndown.md"),
    "tools/judge_backend.py::ask_human_async": ("deliberate", "docs/unreached_burndown.md"),
    "tools/security.py::clear_presented_token": ("deliberate", "docs/unreached_burndown.md"),

    # relay (other) / bridge / scripts.
    "bridge/session_store.py::search_turns": ("deliberate", "docs/unreached_burndown.md"),
    "relay/project_memory.py::list_themes": ("deliberate", "docs/unreached_burndown.md"),
    "relay/project_memory.py::authorities_in": ("deliberate", "docs/unreached_burndown.md"),
    "relay/solve_policy.py::plan_and_explain": ("deliberate", "docs/unreached_burndown.md"),
    "relay/solve_policy.py::finalize": ("deliberate", "docs/unreached_burndown.md"),
    "relay/relay_fleet.py::connector_proven": ("deliberate", "docs/unreached_burndown.md"),
    "relay/fleet_toolset.py::unknown_tools": ("deliberate", "docs/unreached_burndown.md"),
    "relay/turn_outcome.py::classify_turns": ("deliberate", "docs/unreached_burndown.md"),
    "relay/turn_outcome.py::is_capacity_signal": ("deliberate", "docs/unreached_burndown.md"),
    "relay/execution_profiles.py::validate_runtime": ("deliberate", "docs/unreached_burndown.md"),
    "relay/chathub.py::collect_text": ("deliberate", "docs/unreached_burndown.md"),
    "relay/review_resilience.py::looks_like_capability_failure": ("deliberate",
                                                                  "docs/unreached_burndown.md"),
    "relay/review_resilience.py::looks_like_output_filter": ("deliberate",
                                                              "docs/unreached_burndown.md"),
}

#: The inventory as it stood when the reason requirement went in. See the file's own header.
FROZEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unreached_frozen.txt")


def _tracked_anything():
    """Every path git tracks, of any type. None when git cannot answer."""
    import subprocess

    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, timeout=60)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return {ln.strip().replace(chr(92), "/")
            for ln in out.stdout.decode("utf-8", "replace").splitlines() if ln.strip()}


def frozen_inventory() -> set[str]:
    out = set()
    for line in io.open(FROZEN_FILE, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def test_the_frozen_snapshot_is_intact():
    """A snapshot that can be edited is not a snapshot. If this number moves, somebody has
    reopened the past to make the present pass."""
    frozen = frozen_inventory()
    assert len(frozen) == 77, (
        "the 2026-09-13 freeze held 77 names and now holds %d" % len(frozen))
    # GROWTH IS ALLOWED ONLY THROUGH REASONS. Forbidding it outright made the REASONS table
    # unreachable -- the first entry that needed one could not be added at all -- which is a
    # gate that prevents the procedure it exists to enforce.
    unexplained = sorted((BASELINE - frozen) - set(REASONS))
    assert not unexplained, (
        "the inventory has grown past the freeze without going through REASONS: %s"
        % ", ".join(unexplained))


def test_a_new_entry_says_which_question_it_answers():
    """THE ESCAPE HATCH, CLOSED. Pasting a name into the inventory is the cheapest way to make
    the ratchet green, and it leaves no trace that distinguishes it from a triaged entry."""
    added = sorted(BASELINE - frozen_inventory())
    missing = [k for k in added if k not in REASONS]
    assert not missing, (
        "added to the inventory with no reason -- each needs (%s) and a path that shows it, "
        "or a caller, or deletion: %s" % ("|".join(ALLOWED_REASONS), ", ".join(missing)))


def test_every_reason_is_one_of_the_three():
    bad = sorted(k for k, (why, _p) in REASONS.items() if why not in ALLOWED_REASONS)
    assert not bad, "not one of %s: %s" % (ALLOWED_REASONS, ", ".join(bad))


def test_every_reason_points_at_something_that_exists():
    """The half that cannot be satisfied by typing. A reason with no location is the form
    "we decided" takes when nobody can say who."""
    # EVERY TRACKED FILE, NOT ONLY THE PYTHON ONES. U.tracked_files() is `git ls-files *.py`
    # because that is what the scanner needs; a pointer is just as likely to be a .ps1 that
    # calls the function or the document where the decision is written down. Checking the
    # narrower list rejected `docs/unreached_burndown.md` as if it did not exist.
    have = _tracked_anything()
    if have is None:
        pytest.skip("git could not list the tracked files here")
    bad = sorted("%s -> %s" % (k, ptr) for k, (_why, ptr) in REASONS.items()
                 if (ptr or "").split("::")[0] not in have)
    assert not bad, "the pointer names nothing the repository tracks: %s" % ", ".join(bad)


def test_every_pointer_actually_names_the_entry_it_excuses():
    """THE STRONGER HALF, taken from the external harness's own exemption guard.

    Its `coverage-exempt.spec.ts` does not ask whether an exemption's glob is well-formed; it
    asks whether the glob still SELECTS A NON-EMPTY SET, "so a renamed suite cannot silently
    fall out of the uninstrumented gate while its exclude goes stale". A pointer that merely
    resolves to a file is the same shape of nothing.

    IT CAUGHT MINE ON ITS FIRST RUN. Both `revealed` entries pointed at
    docs/unreached_burndown.md, which described the blind spot that revealed them and did not
    name either one -- a pointer to a document that does not mention the thing it is excusing.
    The document names them now, and this test is why.

    The check is deliberately weak-but-real: the pointer's text must contain the entry's bare
    name. It cannot tell a genuine dispatch table from a document that mentions the name in
    passing, and pretending otherwise would be the word-count move this file already refuses.
    What it removes is a pointer that has gone stale or was never true.
    """
    have = _tracked_anything()
    if have is None:
        pytest.skip("git could not list the tracked files here")
    bad = []
    for key, (_why, ptr) in REASONS.items():
        name = key.split("::")[-1]
        path = (ptr or "").split("::")[0]
        try:
            text = io.open(os.path.join(REPO, path), encoding="utf-8", errors="replace").read()
        except OSError:
            bad.append("%s -> %s (unreadable)" % (key, ptr))
            continue
        if name not in text:
            bad.append("%s -> %s (does not name %r)" % (key, ptr, name))
    assert not bad, "the pointer does not support the claim: %s" % ", ".join(bad)


def test_no_reason_outlives_its_entry():
    """A reason for a name that has since been wired or deleted is a sentence the next reader
    will take for the current state -- the same failure the ratchet's other half prevents."""
    stale = sorted(set(REASONS) - BASELINE)
    assert not stale, "reasons for names no longer in the inventory: %s" % ", ".join(stale)


def _keys():
    rows = U.scan()
    if rows is None:
        pytest.skip("git could not list the tracked files here")
    return {r[0] for r in rows}


def test_nothing_new_is_built_without_a_caller():
    new = sorted(_keys() - BASELINE)
    assert not new, (
        "these are defined and referenced nowhere in non-test code -- give each one a caller, "
        "or delete it: %s" % ", ".join(new))


def test_a_function_that_gained_a_caller_leaves_the_list():
    """THE HALF THAT MAKES IT A RATCHET. Without it the inventory is a permanent excuse and
    the next reader takes a historical list for the current state."""
    stale = sorted(BASELINE - _keys())
    assert not stale, (
        "these now have a caller (or are gone) -- remove them from the inventory: %s"
        % ", ".join(stale))


def test_the_scanner_can_still_see():
    """A refactor that stops the scan matching anything would make both tests above pass by
    finding nothing at all."""
    assert _keys(), "the scan found nothing, which means it stopped working"


def test_the_inventory_only_names_files_the_repository_tracks():
    """CI sees tracked files and nothing else. An entry for anything else is a debt CI cannot
    find, and the ratchet then fails on its absence -- measured on 2026-09-12 with a path in
    .git/info/exclude."""
    files = U.tracked_files()
    if files is None:
        pytest.skip("git could not list the tracked files here")
    have = set(files)
    missing = sorted(k for k in BASELINE if k.split("::")[0] not in have)
    assert not missing, "the inventory names untracked files: %s" % ", ".join(missing)


def test_the_two_sets_agree_with_the_scan_about_what_is_tested():
    """THE SPLIT WAS HAND-ASSIGNED AND FOUR ENTRIES WERE ON THE WRONG SIDE.

    `NO_CALLER_NO_TEST` opens with "nothing calls them and nothing checks them" and calls
    itself "the starkest" -- which is an argument for deletion, made in the inventory's own
    voice. Being wrong in that direction is not a quiet inaccuracy.

    Two of the four were wrong because the instrument could not see: `tests/` was missing from
    `tools/unreached.ROOTS`, so the "refs in tests" column answered 0 for everything that
    suite covers -- `tools/golden.py::run_trajectory` against the five references in
    tests/test_golden.py, a file that exists for nothing else. The other two were simply filed
    in the wrong set by a person, with the correct count printed beside them all along.

    So the split stops being a judgement anybody has to remember to make. It is the scan's
    number, checked here.
    """
    rows = U.scan()
    if rows is None:
        pytest.skip("git could not list the tracked files here")
    test_refs = {r[0]: r[5] for r in rows}
    tested_but_filed_as_untested = sorted(
        k for k in NO_CALLER_NO_TEST if test_refs.get(k, 0) > 0)
    assert not tested_but_filed_as_untested, (
        "listed as unchecked, but the scan finds tests referencing them -- move to "
        "NO_CALLER_BUT_TESTED: %s"
        % ", ".join("%s (%d)" % (k, test_refs[k]) for k in tested_but_filed_as_untested))
    untested_but_filed_as_tested = sorted(
        k for k in NO_CALLER_BUT_TESTED if k in test_refs and test_refs[k] == 0)
    assert not untested_but_filed_as_tested, (
        "listed as checked, and no test references them: %s"
        % ", ".join(untested_but_filed_as_tested))


def test_the_scan_reads_the_tests_directory_at_all():
    """The missing root, pinned directly. A count that is silently zero for a whole directory
    reads exactly like a real measurement, which is why it survived.
    """
    assert "tests" in U.ROOTS
    rows = U.scan()
    if rows is None:
        pytest.skip("git could not list the tracked files here")
    test_refs = {r[0]: r[5] for r in rows}
    assert test_refs.get("tools/golden.py::run_trajectory", 0) > 0, \
        "tests/test_golden.py exists for this function and the scan cannot see it"


def test_a_helper_under_tests_is_not_reported_as_dead_production_code():
    """`tests/_srcprobe.py` is imported by the suite and is named like production code. Once
    `tests/` entered ROOTS its public functions would have been listed as unreached unless
    `is_test` recognised the DIRECTORY -- a test helper filed as dead production code, which
    is the category error that put job_authority.free_port in this inventory in the first
    place."""
    assert U.is_test("tests/_srcprobe.py")
    assert U.is_test("tests/sub/helper.py")
    assert not U.is_test("tools/golden.py")
    assert not U.is_test("relay/tests_support.py")


def test_the_burndown_report_closes_exactly_the_kinds_that_are_verdicts():
    """`tools/unreached.py` prints a verdict column by reading REASONS from here.

    **新しい種類を足したとき、それが「閉じる」のか「出自を説明するだけ」なのかは
    足した人にしか分からない。**黙って `-- open --` 側に落ちれば、裁定済みの行が
    もう一度調査される（`judge_autonomy` が 456f803 で決着したのに一覧に並んでいたのが
    まさにそれ）。逆に黙って閉じれば、未回答の行が消える — こちらは取り返しがつかない。
    """
    from tools import unreached as U_
    assert U_._SETTLED <= set(ALLOWED_REASONS), (
        "unreached.py closes rows on a reason kind this file does not define: %r"
        % sorted(U_._SETTLED - set(ALLOWED_REASONS)))
    undecided = set(ALLOWED_REASONS) - U_._SETTLED
    assert undecided == {"revealed"}, (
        "a reason kind was added and nobody said whether it CLOSES a burndown row or only "
        "records how it arrived; unreached.py is currently treating %r as still open"
        % sorted(undecided))
