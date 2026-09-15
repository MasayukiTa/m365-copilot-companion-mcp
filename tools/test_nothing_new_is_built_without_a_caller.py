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
    "relay/lean_capture.py::capture_fn",                           # 10 lines, revealed 2026-09-14
    "tools/golden.py::run_trajectory",                           # 64 lines
    "relay/autonomy_gate.py::judge_autonomy",                    # 54 lines
    "bench/skill_use_log.py::compare_runs",                      # 23 lines
    "tools/judge_backend.py::ask_human_async",                   # 18 lines
    "relay/selfimprove/guards.py::launch_detached",              # 16 lines
    "relay/selfimprove/run_archive.py::revisions",               # 14 lines
    "bridge/session_store.py::compact",                          # revealed 2026-09-13
    "relay/selfimprove/episode_record.py::compact",              # revealed 2026-09-13
    "tools/security.py::get_client_ip",                          # 12 lines
    "scripts/collect_lens_corpus.py::all_inconclusive",          # 8 lines
    "bench/companionbench/job_authority.py::free_port",          # 7 lines
    "scripts/collect_lens_corpus.py::load_corpus",               # 7 lines
    "relay/autonomy_gate.py::constraints_text",                  # 5 lines
    "tools/security.py::is_trusted_local",                       # 3 lines
    "relay/review_resilience.py::looks_like_capability_failure", # 2 lines
    "relay/review_resilience.py::looks_like_output_filter",      # 2 lines
    "relay/review_resilience.py::looks_like_transient_error",    # 2 lines
}

#: Called by nothing outside tests. Tests referencing a function say it was worth writing; they
#: do not say anything reaches it in production.
NO_CALLER_BUT_TESTED = {
    # DEAD SUBGRAPHS, revealed 2026-09-14 by iterating the scan to a fixed point. Each was held
    # off the list by a caller that is itself unreached, so a reference count said "someone
    # names this" while nothing could get there. Not new code.
    "relay/selfimprove/diversify.py::diversify",                    # 62 lines
    "relay/solve_policy.py::plan_solve",                            # 56 lines
    "tools/coding_ops.py::worktree_remove",                         # 44 lines
    "relay/selfimprove/calibration.py::recommend_effort",           # 33 lines
    "relay/selfimprove/propose.py::mutation_generator",             # 33 lines
    "tools/coding_ops.py::worktree_add",                            # 29 lines
    "relay/selfimprove/guards.py::classify_outcome",                # 21 lines
    "bench/companionbench/shadow_rules.py::verdict",                # 20 lines
    "bench/skill_use_log.py::observe",                              # 16 lines
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
    "relay/selfimprove/solver_feedback.py::tally",                 # 27 lines, revealed 2026-09-14
    "relay/selfimprove/authority_ledger.py::verify",               # 25 lines, revealed 2026-09-14
    "relay/selfimprove/decision.py::summarise",                    # 21 lines, revealed 2026-09-14
    "relay/selfimprove/apply.py::apply_genome",                    # 20 lines, revealed 2026-09-14
    "relay/turn_outcome.py::summarise",                            # 13 lines, revealed 2026-09-14
    "relay/selfimprove/harness_feedback.py::report",               # 11 lines, revealed 2026-09-14
    "relay/selfimprove/coreset.py::summarise",                     # 8 lines, revealed 2026-09-14
    "tools/coding_ops.py::survey_worktrees",                     # 96 lines
    "relay/provenance.py::adjudicate",                           # 82 lines
    "relay/selfimprove/propose.py::propose_candidates",          # 68 lines
    "relay/selfimprove/routing.py::held_out_advantage",          # 65 lines
    "bench/companionbench/baseline.py::why_they_flip",           # 54 lines
    "relay/selfimprove/autonomy.py::raise_to",                   # 48 lines
    "relay/bestofn_run.py::load_candidate_dir",                  # 39 lines
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
    "relay/selfimprove/compare.py::withdraw",                    # 20 lines
    "scripts/stale_server_check.py::decide_post_update_action",  # 20 lines
    "relay/selfimprove/diversify.py::diversity_report",          # 17 lines
    "relay/selfimprove/harness_tree.py::justified",              # 17 lines
    "relay/selfimprove/solver_feedback.py::where_distribution",  # 16 lines
    "bench/companionbench/runner.py::solver_feedback_entries",   # 15 lines
    "relay/project_memory.py::list_themes",                      # 15 lines
    "tools/tool_probe.py::next_probe_instruction",               # 15 lines
    "relay/selfimprove/autonomy.py::lower_to",                   # 14 lines
    "relay/selfimprove/branches.py::materialize_to_file",        # 14 lines
    "relay/selfimprove/harness_tree.py::branches",               # 14 lines
    "relay/selfimprove/l2.py::run_until",                        # 14 lines
    "relay/selfimprove/apply.py::revert",                        # 13 lines
    "relay/turn_outcome.py::classify_turns",                     # 13 lines
    "relay/provenance.py::outranks",                             # 12 lines
    "relay/provenance.py::resolved_value",                       # 12 lines
    "relay/selfimprove/record_summary.py::summary_for",          # 12 lines
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
    "bench/remote/broker_client.py::ping",                       # 2 lines
    "relay/selfimprove/runtime_config.py::active_harness_id",    # 2 lines
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
    "relay/selfimprove/diversify.py::diversify": ("revealed", "docs/unreached_burndown.md"),
    "relay/solve_policy.py::plan_solve": ("revealed", "docs/unreached_burndown.md"),
    "tools/coding_ops.py::worktree_remove": ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/calibration.py::recommend_effort":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/propose.py::mutation_generator":
        ("revealed", "docs/unreached_burndown.md"),
    "tools/coding_ops.py::worktree_add": ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/guards.py::classify_outcome":
        ("revealed", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::verdict":
        ("revealed", "docs/unreached_burndown.md"),
    "bench/skill_use_log.py::observe": ("revealed", "docs/unreached_burndown.md"),
    "tools/env_portability.py::parse_env": ("revealed", "docs/unreached_burndown.md"),
    "relay/project_memory.py::entry_authority": ("revealed", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::old_verdict":
        ("revealed", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::new_verdict":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/calibration.py::competence":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/lean_capture.py::enabled": ("revealed", "docs/unreached_burndown.md"),
    "tools/env_portability.py::classify": ("revealed", "docs/unreached_burndown.md"),

    # Both were always unreached and neither was ever printed: `compact` is defined in
    # two modules, and a colliding name used to be skipped by the scan rather than
    # reported. Found 2026-09-13 when a new `require` silently retired
    # relay/selfimprove/autonomy.py::require the same way.
    "bridge/session_store.py::compact": ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/episode_record.py::compact": ("revealed",
                                                    "docs/unreached_burndown.md"),

    # THE SAME BLIND SPOT, MEASURED AND CLOSED 2026-09-14. `unreached.py` skipped a name
    # defined in more than one module whenever ANY definition was referenced -- the half of
    # the 2026-09-13 fix that was left. 421 definitions sat behind that skip. Attributing a
    # reference to the module the AST names reports these eighteen; each was checked against
    # the owning module's own importers before being listed.
    "bench/skill_probe.py::compare":
        ("revealed", "docs/unreached_burndown.md"),
    "bench/companionbench/shadow_rules.py::compare":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/planner_evaluator.py::preflight":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/outcomes.py::tally":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/solver_feedback.py::tally":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/authority_ledger.py::verify":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/decision.py::summarise":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/apply.py::apply_genome":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/turn_outcome.py::summarise":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/harness_feedback.py::report":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/lean_capture.py::capture_fn":
        ("revealed", "docs/unreached_burndown.md"),
    "relay/selfimprove/coreset.py::summarise":
        ("revealed", "docs/unreached_burndown.md"),
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
