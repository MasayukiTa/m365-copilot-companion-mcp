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
    "tools/golden.py::run_trajectory",                           # 64 lines
    "relay/autonomy_gate.py::judge_autonomy",                    # 54 lines
    "bench/skill_use_log.py::compare_runs",                      # 23 lines
    "bench/routing_switch.py::broker",                           # 22 lines
    "scripts/win/capture_budget.py::acknowledge",                # 19 lines
    "main.py::health",                                           # 18 lines
    "tools/judge_backend.py::ask_human_async",                   # 18 lines
    "relay/selfimprove/guards.py::launch_detached",              # 16 lines
    "relay/selfimprove/run_archive.py::revisions",               # 14 lines
    "tools/security.py::get_client_ip",                          # 12 lines
    "relay/fleet_retention.py::open_maybe_gz",                   # 10 lines
    "scripts/collect_lens_corpus.py::all_inconclusive",          # 8 lines
    "tools/lock_state.py::token_gap",                            # 8 lines
    "bench/companionbench/job_authority.py::free_port",          # 7 lines
    "relay/outcomes.py::is_retryable",                           # 7 lines
    "scripts/collect_lens_corpus.py::load_corpus",               # 7 lines
    "relay/profile_token.py::discard_template",                  # 6 lines
    "relay/autonomy_gate.py::constraints_text",                  # 5 lines
    "relay/profile_token.py::forget_memo",                       # 5 lines
    "relay/edge_recover.py::keeper_profile_marker",              # 3 lines
    "tools/security.py::is_trusted_local",                       # 3 lines
    "relay/review_resilience.py::looks_like_capability_failure", # 2 lines
    "relay/review_resilience.py::looks_like_output_filter",      # 2 lines
    "relay/review_resilience.py::looks_like_transient_error",    # 2 lines
}

#: Called by nothing outside tests. Tests referencing a function say it was worth writing; they
#: do not say anything reaches it in production.
NO_CALLER_BUT_TESTED = {
    "tools/coding_ops.py::survey_worktrees",                     # 96 lines
    "relay/provenance.py::adjudicate",                           # 82 lines
    "tools/contract_gate.py::activate_contract",                 # 69 lines
    "relay/selfimprove/propose.py::propose_candidates",          # 68 lines
    "relay/selfimprove/routing.py::held_out_advantage",          # 65 lines
    "relay/selfimprove/diversify.py::diversify",                 # 62 lines
    "bench/companionbench/baseline.py::why_they_flip",           # 54 lines
    "relay/selfimprove/autonomy.py::raise_to",                   # 48 lines
    "relay/selfimprove/runtime_config.py::revert_active",        # 48 lines
    "relay/bestofn_run.py::load_candidate_dir",                  # 39 lines
    "relay/selfimprove/apply.py::safe_commit",                   # 39 lines
    "bench/companionbench/baseline.py::repeat_suite",            # 36 lines
    "tools/env_portability.py::merge_for_new_machine",           # 35 lines
    "relay/selfimprove/runtime_config.py::reset_to_base",        # 33 lines
    "bench/attempt_snapshots.py::transitions",                   # 32 lines
    "relay/selfimprove/solver_feedback.py::to_hypotheses",       # 31 lines
    "relay/selfimprove/autonomy.py::require",                    # 29 lines
    "tools/coding_ops.py::worktree_scope",                       # 29 lines
    "tools/lock_state.py::locked_since",                         # 29 lines
    "tools/tool_probe.py::verify_probe_reply",                   # 27 lines
    "bridge/session_store.py::search_turns",                     # 26 lines
    "tools/tool_probe.py::classify_probe_reply",                 # 26 lines
    "relay/review_resilience.py::diagnose_after_fresh_replay",   # 25 lines
    "relay/selfimprove/trace_to_eval.py::record_correction",     # 25 lines
    "tools/judge_backend.py::sampling_judge_async",              # 24 lines
    "bridge/session_store.py::latest_attached",                  # 23 lines
    "relay/selfimprove/compare.py::withdraw",                    # 20 lines
    "scripts/stale_server_check.py::decide_post_update_action",  # 20 lines
    "relay/selfimprove/runtime_config.py::pending_swap",         # 19 lines
    "relay/selfimprove/diversify.py::diversity_report",          # 17 lines
    "relay/selfimprove/harness_tree.py::justified",              # 17 lines
    "relay/selfimprove/solver_feedback.py::where_distribution",  # 16 lines
    "bench/companionbench/runner.py::solver_feedback_entries",   # 15 lines
    "relay/project_memory.py::list_themes",                      # 15 lines
    "tools/lock_state.py::locked_recently",                      # 15 lines
    "tools/tool_probe.py::next_probe_instruction",               # 15 lines
    "relay/selfimprove/autonomy.py::lower_to",                   # 14 lines
    "relay/selfimprove/branches.py::materialize_to_file",        # 14 lines
    "relay/selfimprove/harness_tree.py::branches",               # 14 lines
    "relay/selfimprove/l2.py::run_until",                        # 14 lines
    "relay/conv_title.py::disambiguate",                         # 13 lines
    "relay/selfimprove/apply.py::revert",                        # 13 lines
    "relay/turn_outcome.py::classify_turns",                     # 13 lines
    "relay/provenance.py::outranks",                             # 12 lines
    "relay/provenance.py::resolved_value",                       # 12 lines
    "relay/selfimprove/record_summary.py::summary_for",          # 12 lines
    "relay/execution_profiles.py::validate_runtime",             # 11 lines
    "relay/quota_meter.py::sustainable_workers",                 # 11 lines
    "relay/selfimprove/guards.py::partition_outcomes",           # 11 lines
    "relay/fleet_toolset.py::unknown_tools",                     # 10 lines
    "relay/turn_outcome.py::is_capacity_signal",                 # 10 lines
    "scripts/stale_server_check.py::fleet_is_running",           # 10 lines
    "relay/solve_policy.py::finalize",                           # 9 lines
    "relay/solve_policy.py::plan_and_explain",                   # 9 lines
    "tools/lock_state.py::matching_record",                      # 9 lines
    "relay/project_memory.py::authorities_in",                   # 8 lines
    "relay/chathub.py::collect_text",                            # 7 lines
    "bench/ui_goal_lines.py::write_ui_file",                     # 6 lines
    "relay/mechanism_telemetry.py::patch_hash",                  # 4 lines
    "relay/conv_title.py::salvageable",                          # 3 lines
    "relay/relay_fleet.py::connector_proven",                    # 3 lines
    "relay/selfimprove/compare.py::transport_versions_differ",   # 3 lines
    "relay/selfimprove/guards.py::is_domain_general",            # 3 lines
    "relay/transport_policy.py::duplicate_risk",                 # 3 lines
    "relay/transport_policy.py::evolvable_fields",               # 3 lines
    "bench/remote/broker_client.py::ping",                       # 2 lines
    "bench/verdicts.py::is_resolved",                            # 2 lines
    "relay/selfimprove/runtime_config.py::active_harness_id",    # 2 lines
    "tools/security.py::clear_presented_token",                  # 2 lines
}

BASELINE = NO_CALLER_NO_TEST | NO_CALLER_BUT_TESTED


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
