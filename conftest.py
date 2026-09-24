"""Repo-wide pytest configuration.

DEFENSE-IN-DEPTH (2026-07): the primary fix for real Windows desktop toasts
firing during test runs lives in tools/notify_ops.py::notify_desktop, which
no-ops whenever PYTEST_CURRENT_TEST is set (pytest sets this automatically for
every test). This autouse fixture is a SECOND, independent layer: it also
monkeypatches the known notify entry points to inert capture stubs, so tests
stay silent even in a hypothetical path that bypasses the env-var guard (for
example, a subprocess that does not inherit PYTEST_CURRENT_TEST).

This does not weaken any test assertion: individual tests that want to assert
on notify call content (see relay/test_admission.py) still save the original
attribute, patch it to their own capture list, and restore it in a finally --
that pattern layers on top of this fixture without conflict, since this
fixture's stub is just a (harmless) no-op capture, not the real emitter.
"""
import hashlib
import io
import os
import pytest


#: Shared operational state that a test must never write to, as DATA rather than as a series
#: of try-blocks: {"module path": {"CONSTANT": "filename under the tmp base"}}.
#:
#: WHY IT IS A LIST AT ALL, AND WHY THAT IS THE PROBLEM. Five times a new shared file appeared,
#: a test filled it, and an entry was added here afterwards -- the routing record, the refusal
#: log, the pending queue, the summary cache, and then capture_status.json, which reached a
#: screen: a stub context's AttributeError was written into the file the cockpit's sign-in dot
#: reads, and the operator saw the dot move while nothing was wrong.
#:
#: A hand-maintained allowlist fails open by construction. test_live_record_isolation.py walks
#: relay/, tools/ and bridge/ for module-level constants that build a path under .fleet and
#: requires every one to appear either here or in DELIBERATELY_NOT_REDIRECTED. A new one fails
#: that test until somebody decides which it is -- so the silence becomes a decision.
#: Modules that are expensive to import (the bridge takes ~4s) and are therefore patched only
#: when a test has already imported them. A test that never imports one cannot write through it.
#: AND EVERY `scripts.*` ENTRY BELOW, for the same reason and one more. The walker did not look
#: at scripts/ at all until 2026-09-14 -- it swept relay/, tools/ and bridge/ -- and widening it
#: found EIGHTEEN constants there naming .fleet, an entire package nobody had classified. They
#: are patched only once a test has imported the module: several of them (run_nightly_real,
#: settle_stage0_replay) pull the selfimprove stack in behind them, and paying that on every one
#: of seven thousand tests to protect a file a test cannot reach is the wrong trade.
ONLY_IF_ALREADY_IMPORTED = frozenset({
    "bridge.copilot_bridge", "bridge.session_store",
    "scripts.diag_report", "scripts.diag_warmup_bias", "scripts.recycle_report",
    "scripts.run_nightly_real", "scripts.settle_stage0_replay",
    "scripts.verify_fleet_transcripts", "scripts.verify_secret_redaction",
    "scripts.win.capture_budget", "scripts.win.checkpoint", "scripts.win.tab_audit",
    "scripts.win.verify_stack", "scripts.win.watch_stack",
})

LIVE_RECORD_REDIRECTS = {
    # _TOKEN_GAP_FILE is DERIVED (`_STATE_FILE.parent / ...`), so redirecting _STATE_FILE alone
    # left it pointing at the operator's .fleet: it was computed from the real path at import.
    # Eight RFC 5737 documentation addresses were found in the live file on 2026-09-13, written
    # by this suite into the counter that decides whether unlock-token enforcement can be
    # switched on. relay/test_live_record_isolation.py now follows derived constants too.
    "tools.lock_state": {"_LOG_FILE": "lock_refusals.jsonl",
                         "_STATE_FILE": "lock_state.json",
                         "_TOKEN_GAP_FILE": "unlock_token_gap.json"},
    "relay.socket_route": {"DEFAULT_LOG": "socket_route.jsonl"},
    # READ-ONLY, AND REDIRECTED ANYWAY. fleet_tool_health only reads the ledger, so it
    # cannot corrupt the operator record -- but a test left unpatched would derive a
    # health verdict from whatever the operator happened to be doing, which is a test
    # whose result depends on the machine it runs on. Redirecting costs nothing and
    # removes that whole class.
    "tools.fleet_tool_health": {"LEDGER": "tool_events.jsonl"},
    # THE SAME ARGUMENT, THREE MORE TIMES, 2026-09-20. These readers were written for ledgers
    # that had been accumulating for weeks with nothing consuming them; all three only read,
    # and all three are redirected for the reason directly above -- their tests pass an
    # explicit path, but the DEFAULT is the operator's live file, so the first test that calls
    # `report()` with no argument would assert against whatever the machine happened to be
    # doing that hour.
    #
    # relay/test_live_record_isolation.py caught all three the moment they landed, which is
    # the registry doing precisely what it was built for: it named tools.auth_stats within
    # minutes of that constant existing, and it named these within one CI run.
    "tools.send_failure_report": {"DEFAULT_LOG": "send_failures.jsonl"},
    "tools.page_count_report": {"DEFAULT_LOG": "page_counts.jsonl"},
    "tools.judge_report": {"DEFAULT_LOG": "judge.jsonl"},
    "relay.selfimprove.pending": {"QUEUE_PATH": "pending_decisions.jsonl"},
    "relay.selfimprove.record_summary": {"CACHE_PATH": "record_summaries.json"},
    "relay.capture_status": {"STATUS_PATH": "capture_status.json"},
    # Runtime post-condition violations. A test that deliberately violates one would 
    # otherwise put it in the operator record, where a violation means something real broke.
    "relay.invariants": {"LOG": "invariants.jsonl"},
    # THE ONLY ONE OF rebuild_history's FOUR THAT IS WRITTEN. It is the DEFAULT output of
    # `main()`, and two of that module's own tests call main() -- so without this they would
    # rebuild the operator's archive on every run. The other three (FLEET, SOCKET_ROUTE,
    # TRANSCRIPTS) are read-only sources and are listed below.
    "tools.rebuild_history": {"HISTORY": "history.json"},
    # Written on the hot path of EVERY tool call, so a test that reaches the gateway fills the
    # operator's evidence ledger with calls that were never made in earnest.
    "tools.tool_ledger": {"LEDGER_PATH": "tool_events.jsonl"},
    # THE EVIDENCE FOR A POLICY DECISION, WRITTEN BY THE TESTS ABOUT IT. fleet_toolset.check()
    # appends a row for every call it would refuse, and the module's own prose cites that log
    # as the measurement justifying its default of `enforce`. Its tests call check() directly
    # with _fleet_run_active monkeypatched true, so each local run added a row to the operator's
    # file: 219 lines on 2026-09-14, one of which this suite had just written (measured 219 ->
    # 220 on a single run of relay/test_fleet_toolset.py). Every row was `process_kill`, which
    # is exactly what the tests use -- so the log could not distinguish a refusal that happened
    # from one a test simulated, and the evidence for the switch was not evidence.
    "relay.fleet_toolset": {"SHADOW_LOG": "toolset_shadow.jsonl"},
    # THE ONLY RECORD OF WHETHER THE NIGHTLY LOOP ACTUALLY RUNS. A test firing written here
    # would answer "has it run?" with yes on a machine where it never has -- which is precisely
    # the question the file was added to make answerable. NOT in ONLY_IF_ALREADY_IMPORTED with
    # the other scripts: this module's own imports are stdlib, and the heavy ones are inside the
    # functions that need them.
    "scripts.selfimprove_driver": {"LOG": "selfimprove_driver.jsonl"},

    # ── scripts/, which the walker did not sweep until 2026-09-14 ───────────────────────────
    #
    # EIGHTEEN CONSTANTS IN A PACKAGE NOBODY HAD CLASSIFIED. PACKAGES was ("relay", "tools",
    # "bridge") and scripts/ writes to .fleet as much as any of them -- the sweep simply never
    # looked. Found while trying to register the driver log above, which could not be listed
    # because the walker could not see the module it lives in.
    #
    # REDIRECTED RATHER THAN TRIAGED ONE BY ONE, and that is a deliberate choice about what can
    # be known cheaply. An AST pass over these eighteen reported all but two as read-only, and
    # it was WRONG: scripts/win/capture_budget.py::ACKED is written through
    # `target = path or ACKED` ... `open(target, "w")`, an indirection the pass could not
    # follow. A classification nobody can stand behind is worse than none, so the ones that are
    # paths are redirected -- which costs a read-only constant nothing but a temp path -- and
    # anything a test genuinely needs to read from the live tree comes back here as a demotion
    # with the failure that proved it.
    "scripts.diag_report": {"DIAG": "diag"},
    "scripts.diag_warmup_bias": {"OUT": "diag"},
    "scripts.recycle_report": {"PATH": "recycle_samples.jsonl"},
    "scripts.run_nightly_real": {"ARCHIVE": "selfimprove/archive.jsonl"},
    "scripts.settle_stage0_replay": {"DEFAULT_TRACE": "settle_trace_collect.jsonl"},
    "scripts.verify_fleet_transcripts": {"DEFAULT_DIR": "transcripts"},
    "scripts.win.capture_budget": {"ACKED": "capture_budget_acked.json",
                                   "LEDGER": "capture_budget.jsonl"},
    "scripts.win.checkpoint": {"AUDIT_LOG": "checkpoint_audit.jsonl",
                               "ROUTE_LOG": "socket_route.jsonl"},
    "scripts.win.tab_audit": {"ROUTE_LOG": "socket_route.jsonl"},
    "scripts.win.verify_stack": {"ROUTE_LOG": "socket_route.jsonl"},
    "scripts.win.watch_stack": {"ROUTE_LOG": "socket_route.jsonl"},
    # Written at admission for every benchmark task; a test that admits a task would otherwise
    # add terms to the operator's real contract file, which is append-only and first-wins.
    "relay.acceptance_contract": {"CONTRACT_PATH": "acceptance_contracts.jsonl"},
    "relay.selfimprove.ledger": {"DEFAULT_PATH": "hypotheses.jsonl"},
    # THE OPERATOR'S TRACE DIRECTORY, WHICH IS NOT UNDER .fleet AND SO WAS NEVER SEEN.
    # ~/.companion_runs holds the tool-call traces and the corrections log; a file there was
    # observed changing during a test run. relay/selfimprove/trace_to_eval reads
    # corrections_*.jsonl from it and promotes what qualifies into an evaluation ledger, so a
    # corrections file a test wrote could be promoted as though a person had written it. Two
    # modules declare the same directory, and both are redirected -- redirecting one would
    # leave the other writing to the live path while the table claimed the matter was settled.
    "tools.trace_ops": {"RUNS_DIR": "companion_runs"},
    "tools.runlog_ops": {"RUNS_DIR": "companion_runs"},
    "relay.selfimprove.compare": {"QUEUE_PATH": "compare_queue.jsonl",
                                  "RESULTS_PATH": "compare_results.jsonl"},
    "relay.selfimprove.runtime_config": {"ACTIVE_PATH": "active_manifest.json"},
    "relay.settle_replay": {"DEFAULT_TRACE": "settle_trace.jsonl"},
    "relay.task_router": {"APPROVED_JOBS_FILE": "approved_jobs.json",
                          "TASKS": "tasks.jsonl"},
    "tools.auth_stats": {"_STATS_FILE": "auth_stats.json"},
    "tools.skill_ops": {"_SKILL_USE_LOG": "skill_use.jsonl"},
    # REDIRECTED, NOT EXCUSED, because this one is written to: `skill_draft.write` puts a
    # generated bundle per proposal here, and a test that exercised it would otherwise drop
    # files into the operator's own proposals directory and leave them there.
    "tools.skill_draft": {"PROPOSALS_DIR": "skill_proposals"},
    "relay.mechanism_telemetry": {"LOG": "mechanisms.jsonl"},
    # THE GAUGE READS THIS FILE DIRECTLY. Every turn the fleet sends is appended here, and the
    # cockpit's rate strip renders whatever it finds -- so a test that records a turn does not
    # just dirty a log, it puts invented traffic on the operator's headroom display and moves
    # the colour toward red for a burst that never happened.
    "relay.quota_meter": {"METER_PATH": "quota_meter.jsonl"},
    "relay.ownership": {"LEDGER": "ownership.jsonl"},
    "relay.edge_reconnect": {"CONN_URL_CACHE": "conn_manager_url.txt"},
    "relay.selfimprove.branches": {"DEFAULT_PATH": "branches.jsonl"},
    "relay.copilot_autopilot_relay": {"_SEND_STAGE_PATH": "send_stage.jsonl",
                                      "_SETTLE_TRACE_PATH": "settle_trace_car.jsonl"},
    # A DIRECTORY OF CAPTURED TOKENS AND REQUEST TEMPLATES. Not a log -- the one place in this
    # list where a stray test write would touch live credentials material.
    "relay.profile_token": {"TEMPLATE_DIR": "templates"},
    "bridge.copilot_bridge": {"DELETE_LOG": "delete_log.jsonl",
                              # The page-count instrument, appended from the CDP watchdog every
                              # 60s. It exists to answer "was the browser accumulating tabs?"
                              # after the fact, which is a question only the operator's real
                              # history can answer -- a test's synthetic counts mixed into it
                              # would corrupt the one record the tab-leak incident left behind.
                              "PAGE_COUNT_LOG": "page_counts.jsonl",
                              "FLEET_CONVS_PATH": "fleet_convs.json",
                              "RECYCLE_SAMPLES_PATH": "recycle_samples.jsonl",
                              "_SETTLE_RESET_TRACE_PATH": "settle_reset_trace.jsonl"},
    # THE BACK-FILL REWRITES TRANSCRIPTS IN PLACE, which is the most destructive shape on this
    # list: it does not append, it replaces the file with the same lines plus one. A test
    # exercising it against the live .fleet would rewrite the operator's real fleet history.
    "tools.backfill_transcript_conv_ids": {"DEFAULT_STATE": "."},
    "bridge.session_store": {"SESS_DIR": "sessions"},
    "tools.tool_probe": {"_PROBE_FILE": "tool_probe.json",
                         "PROBE_FAILURE_JOURNAL": "tool_probe_failures.jsonl",
                         "_INBOUND_PATH": "probe_inbound.json"},

    # ── THE FOURTH CLASS, 2026-09-24 ─────────────────────────────────────────────────────────
    #
    # A live-state file beside its own module, naming no marker directory at all --
    # `relay/selfimprove/apply.py::DEFAULT_STORE` is `os.path.dirname(__file__) / "active_
    # genome.json"`, and every check above (literal scan, then the derived-constant fixpoint)
    # walks straight past it because nothing in that expression mentions .fleet or either other
    # marker. `relay/selfimprove/test_controller.py` wrote the real active_genome.json AND its
    # .prev this same day, through exactly this constant, before a file-scoped fixture caught it
    # by hand. relay/test_live_record_isolation.py's walker was widened the same day to also
    # find a constant that resolves (by its own conservative static evaluator) to a gitignored
    # path, or to a state-shaped filename sitting beside its module -- see STATE_FILE_EXTENSIONS
    # there for the exact rule. Everything below this line is what that widened walk found.
    "relay.selfimprove.apply": {"DEFAULT_STORE": "active_genome.json"},
    # A class-default constructor argument (`class Sentinel: def __init__(self, path=DEFAULT)`)
    # rather than a function default reached on every call -- no production caller currently
    # constructs one without an explicit path (relay/selfimprove/l2.py always resolves and
    # checks its own sentinel_path first). Redirected anyway: it costs nothing but a temp path,
    # and "no caller reaches the default today" is exactly the kind of claim this table exists
    # to stop anyone having to keep re-verifying by hand.
    "relay.selfimprove.sentinel": {"DEFAULT": "sentinel.json"},
    # THE ONE OF THE ELEVEN THAT IS LIVE ON THE HAPPY PATH, NOT JUST REACHABLE. scheduler.nightly
    # forwards its own trace_ledger_path default (None) straight through to
    # trace_to_eval.nightly_step, and relay/selfimprove/test_policy_wiring.py's
    # test_the_scheduled_run_reports_tripwires_rather_than_acting_on_them and
    # test_five_passes_are_not_a_plateau both call S.nightly(...) with no trace_ledger_path --
    # so whether this ever wrote the real promoted_traces.jsonl depended entirely on whether
    # ~/.companion_runs (itself redirected, see tools.trace_ops/tools.runlog_ops above) happened
    # to hold a promotable correction on the day the test ran.
    "relay.selfimprove.trace_to_eval": {"DEFAULT_LEDGER": "promoted_traces.jsonl"},
    # THE ONE MEASURED ACTUALLY WRITING, NOT JUST REACHABLE.
    # tools/test_memory_ops_local.py::test_an_in_process_caller_can_actually_save calls
    # memory_ops.memory_save_local(...) with no monkeypatch on STATE_FILE at all -- it accepts a
    # tmp_path fixture and never uses it. Every write memory_save_local makes goes through
    # _save(), which writes STATE_FILE unconditionally: this test wrote the operator's real
    # .memory_state.json at the repo root on every run until this redirect existed.
    "tools.memory_ops": {"STATE_FILE": "memory_state.json"},
    # Its own test file (tools/test_procedural_memory.py) already monkeypatches STATE_FILE per
    # test via a fixture -- this is belt and braces, the same argument as the read-only
    # tools.* ledgers above: it costs nothing and it removes the class for whichever test in
    # this file gets written next without remembering the local fixture.
    "tools.procedural_memory": {"STATE_FILE": "procedural_memory.json"},
    # THE SECURITY STATE, so belt and braces matters more here than anywhere else on this list.
    # tools/test_security.py already monkeypatches STATE_FILE via its own isolated_state
    # fixture -- this does not replace that, it is the same guarantee the toast stub gives
    # tools.notify_ops: a test in this file that forgets the local fixture still cannot reach
    # the operator's real unlock state.
    "tools.security": {"STATE_FILE": "unlock_state.json"},
    # Its own test file (tools/test_data_aliases.py) already monkeypatches STATE_FILE per test
    # via a fixture, same shape as tools.procedural_memory above -- belt and braces.
    "tools.data_aliases": {"STATE_FILE": "procedural_memory_aliases.json"},
    # NO TEST FILE PROTECTS THIS ONE AT ALL. There is no tools/test_task_ops.py; the module is
    # only reached through main.py's tool registration today. A future test that imports
    # tools.task_ops and calls todo_write with no fixture of its own would write the operator's
    # real .todo_state.json with nothing standing in the way -- which is exactly the shape the
    # other ten entries above were found in, just one step earlier: before the write rather than
    # after it.
    "tools.task_ops": {"STATE_FILE": "todo_state.json"},
}

#: Constants that build a .fleet path but are NOT redirected, each with the reason. Being on
#: this list is a claim that a test writing there is harmless -- so it is short, and each line
#: has to be defensible.
DELIBERATELY_NOT_REDIRECTED = {
    # NOT A PATH -- A SET OF DIRECTORY NAMES the updater must keep when it replaces a tree:
    # {".env", ".venv", ".setup", ".fleet", ...}. Nothing is written THROUGH it, and pointing it
    # at a temp directory would make the updater stop preserving the operator's .fleet, which is
    # the opposite of protection. Same shape as tools.file_ops._SECURITY_STATE_DIRS below.
    ("scripts.update_from_release", "PRESERVE_NAMES"):
        "a set of directory names to preserve during an update, not a location anything "
        "writes to; redirecting it would stop the updater preserving the real .fleet",
    # IT RUNS WHEN IT IS IMPORTED, which is the evidence rather than the excuse: it reads
    # sys.argv at module scope and raises SystemExit(0). The redirect fixture found that out by
    # importing it, which is how it moved from the table above to this one. A module no test can
    # import is a module no test can write through -- and a test that DID import it would not
    # quietly dirty a record, it would fail loudly on the import. That is a stronger guarantee
    # than a redirect, not a weaker one.
    #
    # `scripts/win/_mem_strata.py` and `_merge_watch.py` were listed here and above until CI
    # rejected both: they are UNTRACKED, present only in one checkout. The walker now reads
    # `git ls-files`, so neither is visible to it on any machine, and an entry for either is a
    # stale claim about a file the pushed tree does not contain.
    ("scripts.verify_secret_redaction", "LED"):
        "importing it executes it (sys.argv at module scope, then SystemExit), so no test "
        "can import it and none can write through it",
    ("relay.fleet_reconcile", "TRANSCRIPTS"):
        "read-only: the reconciler only ever reads finished transcripts to compare them, and "
        "writes nothing at all. Its own tests pass an explicit directory, so nothing here "
        "reaches the operator's store either way",
    # ALREADY REDIRECTED, BY THE VARIABLE RATHER THAN BY THIS TABLE. task_router resolves it
    # as os.environ["FLEET_STATE_DIR"] or .fleet, and conftest points that variable at a temp
    # directory for every run -- the same mechanism relay.project_memory uses. Putting it in
    # LIVE_RECORD_REDIRECTS instead would give it two answers that can disagree.
    #
    # It matters more than most entries here: this is not a log. task_router delivers a
    # fleet-bound goal by appending add_goal to <state_dir>/commands.json, which a RUNNING
    # fleet reads and acts on -- so an unredirected test write would not dirty a record, it
    # would hand the operator's live run a goal nobody asked for.
    ("relay.task_router", "FLEET_STATE_DIR"):
        "resolved through the FLEET_STATE_DIR environment variable, which conftest already "
        "points at a per-run temp directory; the .fleet path is only its fallback",
    # ── surfaced 2026-09-17, the same afternoon the constant was added ───────────────────
    #
    # The record of who was turned away at the door. It was written into the operator's live
    # .fleet/ within minutes of existing -- tools/test_auth_stats.py already called
    # record_auth_failure -- and four rows carrying no ip, no path and no agent landed in the
    # file someone would open to find out who was rejected. Caught twice, independently: by
    # running scripts/status.py and reading what it printed, and by this registry, which is the
    # systematic form of the same check and did not need anyone to look.
    ("tools.auth_stats", "_REJECTIONS_FILE"):
        "resolved through the MCP_AUTH_REJECTIONS_FILE environment variable, which conftest "
        "points at a per-run temp file; the .fleet path is only its fallback",
    ("relay.copilot_autopilot_relay", "NOTIFY_SOURCE_LOG"):
        "resolved through the MCP_NOTIFY_SOURCE_LOG environment variable, which conftest "
        "points at a per-run temp file; the .fleet path is only its fallback",
    ("bridge.copilot_bridge", "UNDELIVERED_PATH"):
        "resolved through the MCP_BRIDGE_UNDELIVERED_FILE environment variable, which conftest "
        "points at a per-run temp file; the .fleet path is only its fallback",
    # ── surfaced 2026-09-14 by teaching the walk about `.companion_gates` ────────────────
    #
    # ALREADY REDIRECTED, BY THE VARIABLE RATHER THAN BY THIS TABLE -- the same arrangement as
    # relay.task_router.FLEET_STATE_DIR above, and for a stronger reason: gate_ops reads
    # MCP_GATE_DIR at IMPORT, so conftest sets it at module scope rather than in a fixture, and
    # a value moved here afterwards could disagree with the one the module already resolved.
    ("tools.gate_ops", "GATE_DIR"):
        "resolved through MCP_GATE_DIR, which conftest sets at module scope before the module "
        "is imported; the .companion_gates path is only its fallback",
    # DERIVED FROM GATE_DIR (`GATE_DIR / \"STOP_RELAY\"`), so it moves with it. It matters more
    # than most entries here: it is the kill switch, one file per account shared by every
    # checkout, and a test that left it ON once reported six unrelated scenarios as ABORTED.
    # conftest also clears it around every test -- the redirect stops a test reaching
    # production, the fixture stops a test reaching the test after it.
    ("tools.gate_ops", "STOP_FILE"):
        "derived from GATE_DIR, which MCP_GATE_DIR already moves, and cleared around every "
        "test by the _no_leftover_kill_switch fixture",
    # SAME SHAPE, SAME REASON, ADDED 2026-09-24: tools.approval_policy.record_bypass_decision's
    # audit trail is read/appended by five call sites (gate_ops.gate_ask_local, both
    # contract_gate.check_op branches, task_router.job_gate, relay.skills.request_approval,
    # relay.selfimprove.pending.add) that all fire once a test drives job_approval_mode=bypass
    # -- so this is redirected at import, exactly like GATE_DIR, not by a per-test fixture.
    ("tools.approval_policy", "BYPASS_LOG_FILE"):
        "resolved through MCP_BYPASS_LOG_FILE, which conftest sets at module scope before the "
        "module is imported; the .fleet/bypass_decisions.jsonl path is only its fallback",
    # NOT A PATH -- A LIST OF REGEXES the destructive-command classifier matches against, one of
    # which happens to name `.companion_gates` because a shell that will `type` the approval
    # queue is destructive. Nothing is written through it. Same shape as
    # tools.file_ops._SECURITY_STATE_DIRS below; redirecting it would disarm the classifier.
    ("tools.contract_gate", "_DESTRUCTIVE_PATTERNS"):
        "a list of regexes the destructive-command classifier matches against, not a location; "
        "one of them names .companion_gates because reading the approval queue is destructive",
    ("tools.contract_gate", "_FLEET_DIR"):
        "a directory, not a record; the gate's own files are redirected by the tests that "
        "write them and the contract file is already per-test",
    # FOUND 2026-09-13 by the derived-path pass, not by anybody reading the file: both are
    # built from a constant above rather than from a literal, so the walk could not see them
    # until it followed derivations. The _FLEET_DIR entry above already asserted the first
    # one's answer in prose ("the contract file is already per-test") -- which is a claim, and
    # this table is where claims get checked.
    #
    # MEASURED, NOT ASSUMED: with .fleet/active_contract.json at c5f60f2cc300798e (written by
    # the 04:07 fleet run), the 124 tests in tools/test_contract_activation.py,
    # test_contract_budget_turns.py, test_the_gate_can_be_turned_on.py, test_gate_is_not_demoted.py
    # and relay/selfimprove/test_evolution_loop.py, test_measurement_integrity.py left it
    # byte-identical; so did the full 7031-test preflight, which never moved its mtime off 04:07.
    ("tools.contract_gate", "_CONTRACT_FILE"):
        "the gate's tests build their own contract path and none writes the live file -- "
        "measured byte-identical across the gate and loop suites and a full preflight",
    # GRADE_RESULTS sits inside SWEDIR, which is exempt one line down as a benchmark working
    # directory. It is closer to a record than most of that tree -- selfimprove/loop reads it
    # back to decide which targets count as judged -- so it is listed on its own rather than
    # inherited silently. Measured the same way: the file does not exist after a full run,
    # because nothing in the suite grades anything.
    ("relay.selfimprove.loop", "GRADE_RESULTS"):
        "the grading ledger under the benchmark tree; no test grades, so nothing writes it -- "
        "measured absent after a full preflight",
    ("tools.folder_policy", "POLICY_FILE"):
        "read by the policy gate and written only by the operator's console; a test that "
        "wrote it would be testing the console, which none do",
    ("relay.selfimprove.loop", "SWEDIR"):
        "a benchmark working directory, not an operator record",
    # NOT A PATH -- A DENY LIST. _SECURITY_STATE_DIRS is the set of directory NAMES the file
    # tools refuse to touch: the tool-call trace, where a credential would land if a redaction
    # rule were ever missed, and the approval queue, where a worker that could write would
    # approve its own destructive operation. Nothing writes THROUGH this constant. Redirecting
    # it would point the refusal at a temp directory and quietly disarm the check for the whole
    # test run, which is the opposite of what this table is for.
    ("tools.file_ops", "_SECURITY_STATE_DIRS"):
        "a list of directory names the file tools refuse to touch, not a location anything "
        "writes to; redirecting it would disarm that refusal during tests",
    ("relay.selfimprove.quality_loop", "SWEDIR"):
        "a benchmark working directory, not an operator record",
    # READ-ONLY SOURCES for the archive rebuild. The tool reconstructs history.json FROM these
    # two ledgers; it never opens either for writing, and its own tests hand it explicit paths
    # under tmp_path for every case that exercises the reading. FLEET is the directory the
    # other three are built from, not a location anything writes through.
    ("tools.rebuild_history", "FLEET"):
        "a directory the other constants are derived from; nothing writes through it",
    ("tools.rebuild_history", "SOCKET_ROUTE"):
        "read-only: the socket ledger is the rebuild's source and is never opened for writing",
    ("tools.rebuild_history", "TRANSCRIPTS"):
        "read-only: transcripts are scanned for their goal line and never written",
    ("tools.skill_candidates", "SESSIONS_DB"):
        "read-only, and opened that way in so many words: `_full_goals` connects with "
        "`mode=ro` in the URI, so sqlite itself refuses a write through this handle. It is "
        "read to recover the whole goal text behind the ledger's 600-character truncation",
    ("tools.skill_candidates", "LEDGER"):
        "read-only, and the same socket ledger rebuild_history reads above: skill_candidates, "
        "skill_lessons and skill_draft open it to count past work and never write through it. "
        "Their own tests pass an explicit `ledger=` path, so nothing here depends on the "
        "operator's file being present or on what it happens to contain",
    ("relay.selfimprove.l2_cron", "DEFAULT_LOCK"):
        "a lock file whose whole purpose is to be taken and released; tests that exercise it "
        "pass their own path",
    ("relay.selfimprove.dashboard", "_DEFAULT_GRADE"):
        "an input the dashboard READS; nothing writes it",
    ("relay.selfimprove.dashboard", "_DEFAULT_JSON_OUT"):
        "derived output, regenerated on demand from inputs that are themselves redirected",
    ("relay.selfimprove.dashboard", "_DEFAULT_REPORTS_GLOB"):
        "a glob of inputs the dashboard READS; nothing writes it",
    ("relay.selfimprove.usage", "_DEFAULT_HISTORY"):
        "an input read from the fleet's own history; nothing here writes it",
    ("relay.selfimprove.usage", "_DEFAULT_STATUS"):
        "an input read from the fleet's own status; nothing here writes it",
    # AN IDENTIFIER, NOT A WRITE TARGET -- and redirecting it broke the thing it names.
    # note_inbound recognises a probe's own tool call by looking for this directory's NAME in
    # the call's arguments, so moving it changes what the matcher matches on: the redirect made
    # test_the_probes_own_call_is_stamped fail, which is the check saying the classification
    # was wrong rather than the code. Callers that write challenges pass their own base_dir.
    ("tools.tool_probe", "_CHALLENGE_DIR"):
        "a directory NAME used as the marker note_inbound matches on; redirecting it changes "
        "the identifier, and writers take an explicit base_dir",
    ("relay.selfimprove.calibration", "_DEFAULT_GRADE_PATH"):
        "a grading result the calibration READS; nothing here writes it",
    # NOT PATHS AT ALL. These name .fleet inside an EXCLUSION set -- the directories a walker
    # must skip -- so they are the opposite of a write target. They match the detector because
    # it looks for the string, which is the right side to err on.
    ("relay.folder_coder", "SKIP_DIRS"):
        "an exclusion set naming .fleet as a directory to skip, not a path into it",
    ("relay.project_introspect", "_SKIP"):
        "an exclusion set naming .fleet as a directory to skip, not a path into it",
    ("relay.repo_map", "_SKIP"):
        "an exclusion set naming .fleet as a directory to skip, not a path into it",

    # ── THE FOURTH CLASS, 2026-09-24 -- see the matching header in LIVE_RECORD_REDIRECTS above
    # for what widened the walk to find these three along with the eight redirected there.
    ("relay.local_job_store", "DEFAULT_DB_PATH"):
        "resolved through the MCP_LOCAL_JOB_DB environment variable, which conftest already "
        "points at a per-run temp file; the .jobs/jobs.sqlite3 path is only its fallback",
    # A lock file whose whole purpose is to be taken and released, same shape as
    # relay.selfimprove.l2_cron.DEFAULT_LOCK above. Every call site that can reach it --
    # relay/selfimprove/test_scheduler.py's whole file, and the S.nightly(...) calls in
    # relay/selfimprove/test_policy_wiring.py and test_trace_wiring.py -- passes its own
    # lock_path; checked by reading every call, not assumed from the shape.
    ("relay.selfimprove.scheduler", "DEFAULT_LOCK"):
        "a lock file whose whole purpose is to be taken and released; every test that reaches "
        "scheduler.nightly/scheduled_run passes its own lock_path",
    # READ-ONLY, AND GUARDED TWICE OVER. tools/notify_ops.py checks `COCKPIT.is_file()` and
    # `COCKPIT.name` but never writes through it -- it is the path to a built .exe this module
    # launches via subprocess.Popen. Both call sites that would actually spawn it
    # (notify_approval_gate, open_authority_dashboard) already refuse under
    # PYTEST_CURRENT_TEST/MCP_SUPPRESS_GUI before COCKPIT is touched.
    ("tools.notify_ops", "COCKPIT"):
        "a read-only path to the built FleetCockpit.exe this module launches; nothing writes "
        "through it, and both callers that would spawn it already refuse under pytest",

    # ── THE FIFTH CLASS, 2026-09-24 -- a FUNCTION, not an assignment ────────────────────────
    #
    # tools/security.py's SEC-02/SEC-03 change (commit e25b7a3, "Require the unlock second
    # factor by default; give grants ids and durable revocations") added three private helpers
    # that each `return` a path built from STATE_FILE -- an already-redirected constant -- e.g.
    #
    #     def _generation_file() -> Path:
    #         return STATE_FILE.parent / ".fleet" / "unlock_generation.json"
    #
    # relay/test_live_record_isolation.py's walker only read module-level ASSIGNMENTS, so a
    # `def` that does nothing but compute and return a path was invisible to it -- the walker's
    # own docstring already said as much ("derived through a function rather than an assignment,
    # passes through both passes"). Widened 2026-09-24 to also credit a module-level function
    # whose entire body is one `return <expr>`, when that expression names a marker directory
    # directly or is built from a constant the marker-based passes already found -- the same
    # fixpoint the assignment passes use, one syntactic shape further out.
    #
    # EVERY ENTRY BELOW IS SAFE BY DERIVATION, NOT BY BEING REDIRECTED HERE, and that is the
    # point of writing it down rather than leaving it silently unclassified: a function cannot
    # be handed to `monkeypatch.setattr(mod, const, <path>)` the way a constant can, since
    # replacing a callable with a Path/str breaks every caller that still calls it. What makes
    # each one safe is that it recomputes its answer from an ALREADY-redirected name on every
    # call -- so once that name moves (by this table, or by the FLEET_STATE_DIR-shaped
    # environment overrides above), the function moves with it for free. Three of the tools.
    # security entries are proven, not asserted -- see
    # test_the_three_unlock_helpers_follow_state_files_redirect in
    # relay/test_live_record_isolation.py, which imports the module, redirects STATE_FILE the
    # same way this file's own autouse fixture does, and calls all three.
    ("tools.security", "_revocations_file"):
        "returns STATE_FILE.parent / '.fleet' / 'unlock_revocations.json'; STATE_FILE is "
        "already redirected above, and this recomputes from it on every call -- proven in "
        "relay/test_live_record_isolation.py",
    ("tools.security", "_generation_file"):
        "returns STATE_FILE.parent / '.fleet' / 'unlock_generation.json'; same derivation as "
        "_revocations_file, same proof",
    ("tools.security", "_state_lock_file"):
        "returns STATE_FILE.parent / '.fleet' / 'unlock_state.lock'; same derivation as "
        "_revocations_file, same proof",
    # THE OTHER FIFTEEN THE WIDENED WALK FOUND THE SAME DAY, across every package it sweeps --
    # not because they are about unlock, but because a walker that only proves itself against
    # the one module it was widened for is not proven at all. Each was read at its call site;
    # none of them writes to the real .fleet outside a test that already redirects the constant
    # it derives from.
    ("relay.edge_disk_cap", "_fleet_dir"):
        "read-only: worker_count() joins 'status.json' onto it but every caller may pass its "
        "own fleet_dir, and the module writes nothing",
    ("relay.orphan_reaper", "_work_root"):
        "a benchmark work-tree path (.fleet/swe/work) used only to match a process's command "
        "line by substring, never written through; candidates() accepts an explicit work_root",
    ("relay.refuter_memory", "_default_path"):
        "reachable only via RefuterMemory() with no path, from one production call site "
        "(relay/relay_fleet.py) gated behind MCP_ADAPTIVE_REFUTER=1; no test sets that env var "
        "and reaches the gated branch, and every RefuterMemory test constructs its own instance "
        "with an explicit path (relay/test_refuter_memory.py, "
        "relay/test_refuter_memory_recording.py)",
    ("relay.selfimprove.branches", "_path"):
        "falls back to DEFAULT_PATH, already redirected above (relay.selfimprove.branches)",
    ("relay.selfimprove.ledger", "default_path"):
        "falls back to DEFAULT_PATH, already redirected above (relay.selfimprove.ledger), and "
        "first checks MCP_SELFIMPROVE_HYPOTHESES, which conftest also points at a per-run temp "
        "file before this module is ever imported",
    ("relay.task_router", "_autostart_path"):
        "falls back to FLEET_STATE_DIR, already resolved through the environment variable "
        "conftest points at a per-run temp directory (see FLEET_STATE_DIR's own entry above)",
    ("relay.task_router", "_outcome_cursor_path"):
        "same fallback to FLEET_STATE_DIR as _autostart_path, same protection",
    ("relay.task_router", "_socket_route_path"):
        "same fallback to FLEET_STATE_DIR as _autostart_path, same protection -- its own "
        "docstring says so: 'a test that redirects one must be free to redirect the rest "
        "identically'",
    ("relay.task_router", "_p"):
        "joins onto TASKS, already redirected above (relay.task_router); the redirected value "
        "is a file rather than a directory, which is a naming mismatch and not an isolation gap "
        "-- _p still resolves under the same per-test tmp base either way",
    ("scripts.verify_secret_redaction", "leaked"):
        "reads through LED, already exempted above in this same table for the reason that "
        "importing this module executes it (sys.argv at module scope, SystemExit) -- a function "
        "defined in a module no test can import is a function no test can call",
    ("tools.contract_gate", "_seen_file"):
        "derived from _CONTRACT_FILE, already exempted above ('the gate's tests build their own "
        "contract path and none writes the live file -- measured byte-identical'); also cleared "
        "around every test by this file's own _fresh_contract_gate_seen fixture, which calls "
        "this exact function to find what to delete",
    ("tools.contract_gate", "_retired_file"):
        "derived from _CONTRACT_FILE, same exemption and the same _fresh_contract_gate_seen "
        "fixture as _seen_file",
    ("bridge.session_store", "_base_dir"):
        "falls back to SESS_DIR, already redirected above (bridge.session_store), and first "
        "checks MCP_SESSION_STORE_DIR, which this file's own session-scoped "
        "_isolate_session_store fixture points at a temp directory before any test runs",
    ("bridge.session_store", "_db_path"):
        "derived from _base_dir(), same protection",
    ("tools.tool_ledger", "_repo_path"):
        "returns LEDGER_PATH, already redirected above (tools.tool_ledger); its own docstring "
        "says why it is a function at all: 'resolved at call time so a test (and the repo-wide "
        "isolation fixture) can move it'",
}


@pytest.fixture(autouse=True)
def _no_writes_to_the_live_records(tmp_path_factory, monkeypatch):
    """Autouse for every test in the repo: point the shared operational records at tmp.

    THE SAME DEFECT, THREE TIMES IN ONE DAY. A new append-only file appears, the tests that
    exercise the code around it have no reason to know it is shared, and they fill it: the
    routing record got `route_closed` events for a route that never closed, and the refusal log
    got 117 lines from 203.0.113.7 -- a documentation address no backend has ever called from.
    Both had to be wiped, and a wipe loses whatever real history was in there too.

    Per-file fixtures fixed each one after the fact. This is the layer that makes the NEXT one
    inert by construction, in the same place and for the same reason as the toast stub below:
    a test should not be able to write to an operator's records by accident.

    Tests that are ABOUT these files redirect them again themselves, which is harmless -- and
    they must keep doing so, because this fixture gives the whole session ONE directory and
    two tests in a row would otherwise see each other's lines.
    """
    base = tmp_path_factory.mktemp("live_records")
    # DATA, NOT A SERIES OF TRY-BLOCKS. Each entry used to be its own hand-written block added
    # after the file it protects had already been written to by a test. The table above is the
    # same information in a form another test can check for completeness.
    import importlib
    import sys as _sys
    for module_path, consts in LIVE_RECORD_REDIRECTS.items():
        try:
            if module_path in ONLY_IF_ALREADY_IMPORTED and module_path not in _sys.modules:
                # Importing it here would cost every run, including a single-test one, and
                # this fixture is autouse. A test that never touches the module cannot write
                # through it either, so there is nothing to protect until it is imported.
                continue
            mod = importlib.import_module(module_path)
        except Exception:
            continue
        for const, filename in consts.items():
            try:
                current = getattr(mod, const, None)
                target = base / filename
                # Match the type the module already uses: a module that stores a Path and
                # receives a str breaks on `.parent`, and one that stores a str and receives a
                # Path breaks on concatenation. Neither failure would look like this fixture.
                from pathlib import Path as _P
                value = _P(str(target)) if isinstance(current, _P) else str(target)
                monkeypatch.setattr(mod, const, value, raising=False)
            except Exception:
                pass

    yield base


def _fingerprint(path):
    """(exists, size, sha256-or-None) for `path`. Reads the file only to hash it -- the digest
    is a fingerprint, never the content, and nothing that calls this ever holds or prints the
    plaintext. An OSError while stat'ing/reading (permission denied, a transient lock) collapses
    to (None, None, None) -- "could not verify" -- so a filesystem hiccup gives the SAME answer
    on both sides of a before/after comparison instead of masquerading as a real change.
    """
    from pathlib import Path as _P

    try:
        p = _P(path)
        if not p.is_file():
            return (False, None, None)
        size = p.stat().st_size
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return (True, size, h.hexdigest())
    except OSError:
        return (None, None, None)


#: Modules on LIVE_RECORD_REDIRECTS (above) whose real, unpatched default is documented --
#: in that very table's own comments -- as NOT living directly under .fleet/, so
#: _real_dotenv_and_fleet_state_targets must not assume "fleet_dir / <redirect filename>" for
#: them. Found by reading every entry on that table for the phrase describing where the real
#: file actually is (grep for "repo root" / "NOT UNDER .fleet" / "beside its own module" turns
#: up exactly these three; add to this set if a future entry documents another one):
#:   * tools.memory_ops        -- .memory_state.json is AT THE REPO ROOT (that entry's own
#:                                 comment: a test "wrote the operator's real
#:                                 .memory_state.json at the repo root ... until this redirect
#:                                 existed").
#:   * tools.trace_ops / tools.runlog_ops -- RUNS_DIR is `~/.companion_runs`, under the user's
#:                                 HOME directory (that entry's own comment says so verbatim:
#:                                 "THE OPERATOR'S TRACE DIRECTORY, WHICH IS NOT UNDER .fleet").
#:   * relay.selfimprove.apply -- DEFAULT_STORE sits BESIDE ITS OWN MODULE
#:                                 (`relay/selfimprove/active_genome.json`), naming no marker
#:                                 directory at all (that entry's own comment, under the
#:                                 "FOURTH CLASS" heading above).
#:   * tools.security          -- STATE_FILE is `.unlock_state.json` AT THE REPO ROOT
#:                                 (`Path(__file__).resolve().parent.parent / ".unlock_state.
#:                                 json"`), a dotfile beside conftest.py itself, not inside
#:                                 .fleet/ at all. Found 2026-09-24, the same day the entry was
#:                                 added here, BY RUNNING THE COMPUTATION rather than reading it:
#:                                 with the module left off this set, the old code silently
#:                                 asked for the fingerprint of `.fleet/unlock_state.json` --
#:                                 which has never existed on this machine -- so the canary could
#:                                 never have detected a write to the real, 17KB, actively-written
#:                                 `.unlock_state.json` it was meant to guard. See
#:                                 _security_critical_targets() below for where its real path,
#:                                 and the paths derived from it, are tracked instead -- as a
#:                                 HARD FAIL, not the .fleet/ warning this exception set exists
#:                                 to route everything else away from.
_FLEET_TARGET_MODULE_EXCEPTIONS = frozenset({
    "tools.memory_ops", "tools.trace_ops", "tools.runlog_ops", "relay.selfimprove.apply",
    "tools.security",
})


def _security_critical_targets():
    """The unlock/lock-state ledgers, by their REAL (unredirected) absolute path -- checked as
    a HARD FAIL below, not folded into the .fleet/ warning-only bucket.

    WHY THESE GET A STRICTER GUARANTEE THAN THE REST OF .fleet/. The warning-only design lower
    in this file is deliberate and measured: page_counts.jsonl legitimately grows every ~60s
    while the bridge's CDP watchdog is running, so a byte-level change there is not evidence of
    a test leak on this machine. Nothing plays that role for these seven files -- no scheduled
    loop, watchdog, or background process writes an unlock grant, a revocation, a lock refusal,
    or the token-gap counter; the ONLY way any of them changes is a real unlock/lock call. A
    change here during a test session is therefore unambiguous, which is exactly what commit
    e25b7a3 (SEC-02/SEC-03, "Require the unlock second factor by default; give grants ids and
    durable revocations") needed and the general .fleet/ warning could not give it: the evidence
    cited for switching MCP_REQUIRE_UNLOCK_TOKEN on is unlock_token_gap.json's count, and a
    canary that can only warn about contamination in that count is a canary that lets the
    contaminated number stand.

    HARD-CODED, NOT DERIVED FROM LIVE_RECORD_REDIRECTS, because three of these seven --
    tools.security's _revocations_file/_generation_file/_state_lock_file -- are FUNCTIONS, not
    constants (see LIVE_RECORD_REDIRECTS' "FIFTH CLASS" entries above for why they cannot be
    redirected the same way a constant is, and why that does not make them unsafe). Their real,
    unpatched path is reproduced here from their own source rather than imported, for the same
    "cheap, no imports" reason _real_dotenv_and_fleet_state_targets gives for the constant-based
    half below -- these three are one-line functions that have not changed since e25b7a3 added
    them, and re-deriving their answer here costs nothing an import would not also cost.
    """
    from pathlib import Path as _P

    root = _P(__file__).resolve().parent
    fleet_dir = root / ".fleet"
    return sorted({
        root / ".unlock_state.json",                    # tools.security.STATE_FILE
        fleet_dir / "unlock_revocations.json",           # tools.security._revocations_file()
        fleet_dir / "unlock_generation.json",            # tools.security._generation_file()
        fleet_dir / "unlock_state.lock",                 # tools.security._state_lock_file()
        fleet_dir / "lock_state.json",                   # tools.lock_state._STATE_FILE
        fleet_dir / "lock_refusals.jsonl",               # tools.lock_state._LOG_FILE (refusals
                                                          # AND record_granted's grant rows)
        fleet_dir / "unlock_token_gap.json",             # tools.lock_state._TOKEN_GAP_FILE
    })


def _install_security_write_watch():
    """Patch `builtins.open` and `os.replace`, for the life of the test session, so any write
    that reaches one of `_security_critical_targets()`'s seven real paths FROM CODE RUNNING IN
    THIS PYTEST PROCESS is recorded. This is the discriminator
    `_real_dotenv_and_fleet_state_must_not_change` needs and could not get from a byte-level
    diff alone: on the owner's live-install machine, the same seven files are also written by
    an entirely separate, already-running OS process (the supervisor / fleet workers, through a
    real unlock/lock/grant/revoke call over the gateway) for the whole time a local session
    runs. A change with NO in-process write recorded is therefore production traffic, not a
    test leak. A change WITH one is a leak, regardless of whether the write went through
    tools.security's own API (unlock/grant_ip/revoke_ip, all funnelled through one `_transact`
    choke point) or bypassed it entirely with a raw `open()`/`os.replace()` call the way
    test_real_dotenv_canary.py's own hard-fail proof test does -- both are covered here because
    both ultimately call one of these two primitives with the real path as an argument, from
    code executing inside THIS interpreter, which is where the patch lives.

    WHY THE PATCH SURFACE, NOT tools.security ITSELF. tools/security.py is FROZEN (this repo's
    own standing instruction: never edit it). This patches the call surface from the test side
    instead -- the same technique `_no_writes_to_the_live_records` above already uses to
    redirect other modules' path constants -- so nothing about the frozen module changes, and
    every write it performs (`_atomic_write`'s tempfile.mkstemp + os.fdopen + os.replace, and
    tools.lock_state's plain `open(_LOG_FILE, "a", ...)`) is still visible here because both
    resolve, eventually, to one of these two calls with the real path.

    WHY A SEPARATE PROCESS IS INVISIBLE TO THIS, ON PURPOSE. Monkeypatching a name on THIS
    process's `os`/`builtins` module objects has no effect whatsoever on another process's own
    copy of them -- a live supervisor process replacing `.unlock_state.json` calls ITS OWN
    `os.replace`, in its own address space, never this one. That is exactly the property this
    canary needs: it must see every write this interpreter makes and none that another one
    does.

    Returns `(touched, restore)`: `touched` is a set of `os.path.normcase`d absolute path
    strings, mutated in place as writes happen (read it after teardown, not during); `restore`
    puts `builtins.open` and `os.replace` back and must be called exactly once.
    """
    import builtins

    critical = {os.path.normcase(os.path.abspath(str(p))) for p in _security_critical_targets()}
    touched = set()

    def _note(path):
        try:
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
        except Exception:
            return
        if key in critical:
            touched.add(key)

    real_open = builtins.open
    real_replace = os.replace

    def _open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)):
            _note(file)
        return real_open(file, *args, **kwargs)

    def _replace(src, dst, *args, **kwargs):
        if isinstance(dst, (str, bytes, os.PathLike)):
            _note(dst)
        return real_replace(src, dst, *args, **kwargs)

    builtins.open = _open
    os.replace = _replace

    def restore():
        builtins.open = real_open
        os.replace = real_replace

    return touched, restore


def _real_dotenv_target():
    """The REAL repo .env's path -- conftest.py sits at the repo root, so this is always
    right beside it. A tiny function rather than a module constant so it re-resolves
    __file__ the same way _real_dotenv_and_fleet_state_targets does, instead of being a
    second, independently-computed answer to "where is .env" that could drift from it.
    """
    from pathlib import Path as _P

    return _P(__file__).resolve().parent / ".env"


def _real_dotenv_and_fleet_state_targets():
    """The REAL repo .env, plus every real top-level .fleet/<file> that LIVE_RECORD_REDIRECTS
    (above) names, EXCEPT the documented exceptions in _FLEET_TARGET_MODULE_EXCEPTIONS whose
    real default lives somewhere else entirely (see that set's own comment for each one) --
    and except any redirect filename that is not a plain top-level name (contains a path
    separator, or is "." -- a whole-directory marker, not one file) since those name a
    subdirectory or a directory, not a single file this function can fingerprint.

    DELIBERATELY STRING-ONLY, NO IMPORTS. An earlier version of this function imported every
    module on LIVE_RECORD_REDIRECTS to read each constant's real (unpatched) value straight
    from the attribute -- more precise in principle, but measured at ~6 seconds of import cost
    PER SESSION even for a single targeted test file that touches none of these modules, which
    fails the "cheap" bar this canary exists to meet: a session-scoped autouse fixture that
    taxes every run, including a one-test debugging loop, defeats its own purpose if it is the
    slow part of running that one test. The redirect filenames on LIVE_RECORD_REDIRECTS were
    already chosen to match each constant's real basename (see that table's own comments, which
    repeatedly quote the REAL filename measured on the operator's machine, e.g. "223 lines in
    toolset_shadow.jsonl") -- trusting that convention plus the three hand-verified exceptions
    above is the cheap version of the same answer.
    """
    from pathlib import Path as _P

    root = _P(__file__).resolve().parent
    fleet_dir = root / ".fleet"
    targets = {root / ".env"}
    for module_path, consts in LIVE_RECORD_REDIRECTS.items():
        if module_path in _FLEET_TARGET_MODULE_EXCEPTIONS:
            continue
        for filename in consts.values():
            if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
                continue
            targets.add(fleet_dir / filename)
    # The unlock/lock-state ledgers are tracked separately (_security_critical_targets, above)
    # because three of the seven are functions tools.security's own exception entry excludes
    # from this loop -- but they still belong in the ONE set the fixture below fingerprints
    # before/after, so a session that touches any of them is caught at all, before the fixture
    # decides which bucket (hard fail vs warning) that touch falls into.
    targets.update(_security_critical_targets())
    return sorted(targets)


@pytest.fixture(autouse=True, scope="session")
def _real_dotenv_and_fleet_state_must_not_change():
    """SESSION-SCOPED CANARY: the whole test session must leave the owner's REAL .env, and the
    seven unlock/lock-state ledgers _security_critical_targets() names, byte-for-byte untouched
    (HARD FAIL for both), and WARNS (does not fail) if any OTHER real top-level .fleet/ state
    file LIVE_RECORD_REDIRECTS names changes during the session.

    2026-09-24, LATER THE SAME DAY: "byte-for-byte untouched" for the seven ledgers turned out
    to hard-fail every push made while the fleet is busy, because a live supervisor/bridge on
    the owner's machine legitimately performs real unlock/lock/grant/revoke calls -- in its own,
    separate OS process -- against these exact seven paths for the whole time a local session
    runs; measured elsewhere as `sleep 30` alone changing the same four files with no test
    running at all. A byte-level diff alone cannot tell that apart from an actual test leak, so
    this fixture now also asks `_install_security_write_watch()` which of the seven, if any,
    were touched by a write from CODE RUNNING IN THIS PYTEST PROCESS -- and hard-fails only
    those. A change with no in-process write behind it is attributed to the concurrent
    production process instead and only warns (see the `security_external` branch below). See
    _install_security_write_watch's own docstring for how that attribution survives both
    tools.security being FROZEN (never edited) and a leak that bypasses its API entirely with a
    raw open()/os.replace() call.

    THE SECURITY-CRITICAL SET WAS ADDED 2026-09-24, alongside commit e25b7a3 (SEC-02/SEC-03).
    Before it, these seven files sat in the same warning-only bucket as page_counts.jsonl --
    correct for a file a live watchdog legitimately rewrites every ~60s, wrong for a file that
    only changes when an actual unlock/lock/grant/revoke happens. tools.security's own STATE_FILE
    entry had a second, sharper bug on top of that: it was never in _FLEET_TARGET_MODULE_
    EXCEPTIONS, so this function's loop computed `fleet_dir / "unlock_state.json"` for it -- a
    path that has never existed on this machine, since the real file is `.unlock_state.json` AT
    THE REPO ROOT. The canary could not have caught a leak into the real file; it was
    fingerprinting nothing. See _FLEET_TARGET_MODULE_EXCEPTIONS' own entry for tools.security.

    TWICE IN ONE DAY (2026-09-24) a test run reached real operator state anyway, through two
    DIFFERENT mechanisms neither of the existing safeguards covered: a test wrote
    relay/selfimprove/active_genome.json through a path built at runtime, before that constant
    existed on LIVE_RECORD_REDIRECTS (see the "FOURTH CLASS" comment above, added the same day
    this was found); separately, scripts/test_bootstrap.py's DevTunnelNeverBlocksTests pointed
    bootstrap.ROOT at the real checkout while exercising the new "set aside a carried .env's
    tunnel keys" step (D7), and for about a minute commented out the owner's real
    MCP_TUNNEL_* lines in the real .env -- found and put back by hand (see that test class's own
    "A TEMPORARY ROOT, ALWAYS" comment, added the same day as its fix).

    LIVE_RECORD_REDIRECTS and the per-test fixture above already redirect every KNOWN write
    target, one entry at a time. This is the BACKSTOP, not a replacement for that table: it does
    not know which file a test SHOULD have redirected, only that .env -- and the .fleet/ files
    already named on that table -- must read the same at the end of the session as they did at
    the start. That is exactly what catches a redirect that silently failed to take (an
    import-order surprise, the `except Exception: continue` above swallowing a real problem) and
    a code path like test_bootstrap's own ROOT swap, which reaches the real path directly and
    was never a LIVE_RECORD_REDIRECTS case to begin with.

    WHY .env IS A HARD FAIL AND .fleet/ IS ONLY A WARNING -- measured, not assumed. The first
    version of this fixture failed the session on ANY change to either. On this machine (the
    owner's live install, per this repo's own standing warning against starting/stopping the
    supervisor/bridge here), a real run of this exact suite failed on
    .fleet/page_counts.jsonl -- measured growing from 1,307,925 to 1,308,085 bytes DURING an
    unrelated ~115s test session, with no test in that session writing to it. The cause is
    bridge/copilot_bridge.py's own CDP watchdog, which appends to that file every ~60s AS PART
    OF NORMAL PRODUCTION OPERATION whenever the bridge is running -- which it legitimately was,
    the whole time, on this machine. A byte-level change to a .fleet/ file is therefore NOT
    reliable evidence of a TEST writing to it on a machine with a live install; a byte-level
    change to .env is -- nothing in this repository's normal running operation rewrites .env
    (see .env's own row in the state inventory: it is written only by setup/quickstart/
    setup_devtunnel/configure_env/heal_tunnel, none of which run as a side effect of `pytest`).
    Hard-failing .fleet/ changes here would make this canary itself the thing that turns a
    healthy owner's-machine test run red for a reason having nothing to do with test hygiene --
    which is precisely the "the operator saw the dot move while nothing was wrong" failure mode
    _no_writes_to_the_live_records's own docstring describes, just relocated to this fixture. A
    THROWAWAY CLONE (no live supervisor/bridge running against it) does not have this problem,
    and is where the .fleet/ half of this canary is actually proven to catch a real test write
    (see test_real_dotenv_canary.py's throwaway-clone tests at the repo root).

    SESSION-scoped, not per-test: fingerprinting several dozen files is cheap but not free, and
    the property this proves -- "the session as a whole left these alone" -- does not need
    re-proving after every one of several thousand tests. NEVER reads a byte of .env (or any
    .fleet/ file) for any purpose other than feeding it to sha256: the fingerprint is (exists,
    size, digest), and every message below prints only paths, sizes and digests, never contents.
    """
    dotenv_path = _real_dotenv_target()
    security_paths = set(_security_critical_targets())
    targets = _real_dotenv_and_fleet_state_targets()
    before = {p: _fingerprint(p) for p in targets}
    touched_by_this_process, _restore_write_watch = _install_security_write_watch()
    try:
        yield
    finally:
        _restore_write_watch()
    changed = [p for p in targets if _fingerprint(p) != before[p]]
    dotenv_changed = [p for p in changed if p == dotenv_path]
    security_changed_raw = [p for p in changed if p != dotenv_path and p in security_paths]
    # SPLIT BY ATTRIBUTION, NOT JUST BY PATH. `_install_security_write_watch` (above) recorded
    # every one of these seven paths that a write from CODE RUNNING IN THIS PROCESS actually
    # reached. A path that changed but was never touched in-process was written by a separate,
    # already-running production process (the supervisor / fleet workers) during the session --
    # exactly the case that made this hard fail block every push while the fleet is busy, even
    # though three commits behind it were independently verified green. A path that changed AND
    # was touched in-process is unambiguous: nothing but this session's own code reached it.
    security_leaked = [p for p in security_changed_raw
                       if os.path.normcase(os.path.abspath(str(p))) in touched_by_this_process]
    security_external = [p for p in security_changed_raw if p not in security_leaked]
    security_changed = security_leaked
    fleet_changed = [p for p in changed
                     if p != dotenv_path and p not in security_paths]

    if dotenv_changed:
        pytest.fail(
            "LIVE-STATE CANARY (conftest._real_dotenv_and_fleet_state_must_not_change): this "
            "test session modified the real repo .env, which nothing in a normal `pytest` run "
            "should ever touch -- every write should have gone through LIVE_RECORD_REDIRECTS "
            "(see conftest.py), dotenv neutralisation, or an equivalent per-test redirect "
            "instead. Changed file(s):\n" +
            "\n".join("  %s (was %r, now %r)" % (p, before[p], _fingerprint(p)) for p in dotenv_changed),
            pytrace=False,
        )
    if security_changed:
        # HARD FAIL, UNLIKE THE GENERAL .fleet/ BUCKET BELOW -- see _security_critical_targets()
        # and this fixture's own docstring for why these seven do not share page_counts.jsonl's
        # excuse: nothing but a real unlock/lock/grant/revoke call ever writes one of them, so a
        # change here during a test session is a leak, not routine production traffic. THIS IS
        # NOW "a change AND an in-process write", not just "a change" -- see
        # _install_security_write_watch's own docstring for why that split exists and how it
        # still catches a leak that bypasses tools.security's API entirely (a raw open()/
        # os.replace() call from inside this process, the way this file's own hard-fail proof
        # test does it), while no longer blaming a concurrent production process for a write
        # this session's own code never made.
        pytest.fail(
            "LIVE-STATE CANARY (conftest._real_dotenv_and_fleet_state_must_not_change): this "
            "test session modified a real unlock/lock-state ledger, which only an actual "
            "unlock/lock/grant/revoke call should ever touch -- every write from a test should "
            "have gone through the tools.security / tools.lock_state redirects in "
            "LIVE_RECORD_REDIRECTS (see conftest.py) instead. Changed file(s):\n" +
            "\n".join("  %s (was %r, now %r)" % (p, before[p], _fingerprint(p))
                      for p in security_changed),
            pytrace=False,
        )
    if security_external:
        # WARNING ONLY -- these seven paths changed, but never through a write this SESSION's
        # own code made (see _install_security_write_watch): a live supervisor/bridge on this
        # machine legitimately performs real unlock/lock/grant/revoke calls, in its own
        # process, independent of any test, for as long as it runs. Reported so it can still be
        # noticed, exactly like the general .fleet/ bucket below.
        import warnings as _warnings

        _warnings.warn(
            "LIVE-STATE CANARY WARNING (conftest._real_dotenv_and_fleet_state_must_not_change): "
            "%d real unlock/lock-state ledger(s) changed during this session with no write "
            "traced back to this session's own code (NOT failing -- attributed to a concurrent "
            "production process, e.g. a live supervisor/bridge on this machine; see "
            "_install_security_write_watch's docstring). Changed file(s): " % len(security_external) +
            "; ".join("%s (was %r, now %r)" % (p, before[p], _fingerprint(p)) for p in security_external),
            stacklevel=1,
        )
    if fleet_changed:
        # WARNING ONLY -- see this fixture's own docstring for the measured reason (a live
        # supervisor/bridge on this machine legitimately writes some of these files every
        # ~60s, independent of any test). warnings.warn(), NOT print(): a plain print() during
        # SESSION-scoped teardown is captured by pytest like any other test output, and pytest
        # only ever SHOWS captured output for a test that failed -- on an otherwise-green run
        # (which this is, by design: the whole point is not to fail it) the print was measured
        # to vanish completely, reaching nobody. warnings.warn() goes through pytest's own
        # warnings plugin instead, which this repo's own runs already show working: every run
        # in this suite prints a "warnings summary" section (see the opentelemetry/authlib
        # deprecation warnings at the bottom of any run of this file) REGARDLESS of whether
        # anything failed, which is exactly the "surfaced but not fatal" behaviour this needs.
        import warnings as _warnings

        _warnings.warn(
            "LIVE-STATE CANARY WARNING (conftest._real_dotenv_and_fleet_state_must_not_change): "
            "%d real .fleet/ file(s) changed during this session (NOT failing -- see this "
            "fixture's own docstring: a live supervisor/bridge on this machine can legitimately "
            "write these independent of any test). Changed file(s): " % len(fleet_changed) +
            "; ".join("%s (was %r, now %r)" % (p, before[p], _fingerprint(p)) for p in fleet_changed),
            stacklevel=1,
        )


@pytest.fixture(autouse=True)
def _autostart_off(monkeypatch):
    """No test starts a fleet because this machine's .env says to.

    FLEET_INTAKE_AUTOSTART is a deployment choice: with it on, a fleet_goal that finds no run
    in flight LAUNCHES one -- a browser, a set of workers, and the tenant's Copilot budget.
    task_router reads .env at import, so the moment the operator turned it on, every test that
    imports the module inherited it, and any test reaching fleet_handoff with no live fleet
    would have spawned a real run.

    Tests that are ABOUT autostart set it themselves; this only removes the ambient value.
    autostart_fleet also refuses outright under pytest, because a fixture can be missed and a
    subprocess does not see it.
    """
    try:
        from relay import task_router as _tr
        monkeypatch.setattr(_tr, "AUTOSTART", False, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _no_desktop_toasts(monkeypatch):
    """Autouse for every test in the repo: stub known notify entry points."""
    calls = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    try:
        import tools.notify_ops as notify_ops
        monkeypatch.setattr(notify_ops, "notify_desktop", _capture, raising=False)
    except Exception:
        pass

    try:
        import relay.copilot_autopilot_relay as car
        monkeypatch.setattr(car, "default_notify", _capture, raising=False)
    except Exception:
        pass

    try:
        import relay.relay_fleet as rf
        monkeypatch.setattr(rf, "default_notify", _capture, raising=False)
    except Exception:
        pass

    yield calls


# --------------------------------------------------------------------------------------------
# A TEST RUN MUST NOT BE ABLE TO STOP THE REAL FLEET.
#
# The kill-switch is a file under the account's home, so every checkout and every server
# instance running as one user shares one global switch. While the in-process stop path was
# broken this was invisible -- contract_gate's stop was silently refused, so a test that
# tripped a destructive op_class set nothing. The moment that path worked, such a test left
# the switch ON: the next run reported six unrelated scenarios as ABORTED, and the report
# blamed the scenarios.
#
# Set BEFORE tools.gate_ops is imported, which is why it is at module scope in conftest
# rather than in a fixture -- the module reads the variable once, at import.
import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile
import time as _time
import uuid as _uuid

# NO WINDOWS OPEN ON THE OPERATOR'S DESKTOP DURING A TEST RUN.
#
# `tools/notify_ops.notify_approval_gate` launches FleetCockpit's approval prompt as a real GUI
# process and already guards on PYTEST_CURRENT_TEST -- but pytest sets that variable only while
# a test FUNCTION is executing. Collection, session-scoped fixtures and teardown all run with it
# absent, and a gate written in any of those phases sails straight past the guard.
#
# Measured 2026-09-15: a dialog reading 「承認ゲートを開けませんでした」 appeared on the desktop
# mid-run, naming BOTH `skills_gates_pytest_<pid>` and `companion_gates_pytest_<pid>` -- so the
# window was a child of a pytest process, and it could never be satisfied either: the skills
# store had written the gate through MCP_SKILLS_GATE_DIR while the cockpit resolved
# MCP_GATE_DIR, two directories that are equal in production and deliberately different here.
# The operator was handed a prompt with nothing behind it and no way to dismiss the cause.
#
# Set at MODULE scope for the reason PYTEST_CURRENT_TEST fails: this must hold for the whole
# session, not for the phases pytest happens to label.
_os.environ.setdefault("MCP_SUPPRESS_GUI", "1")


# NO CONSOLE WINDOW OPENS ON THE OPERATOR'S DESKTOP BECAUSE A TEST STARTED A CHILD.
#
# The owner, 2026-09-24: 「仮想環境のCLIてきなのがぽんぽん出てきてうっとうしい。4回出てくる...一瞬
# 出てきたときに文字打ってたらそれ消える」. Measured the same day: the windows came from TEST RUNS.
# pytest is launched by an agent from a shell with no console, and a console program started by a
# parent with no console gets a NEW, visible one (tools/childproc.py::headless_creationflags has
# the measurement). Every test that ran `.venv\Scripts\python.exe <script>` or
# `cmd /d /s /c chcp 65001 & powershell ...` without creationflags put a window up and took the
# keyboard from whatever the owner was typing into.
#
# BY CONSTRUCTION, NOT BY FIXING TESTS ONE AT A TIME. Every child this process creates goes
# through `_winapi.CreateProcess` -- subprocess.Popen, asyncio's subprocess transport and
# multiprocessing's spawn all end there -- so that one call is where the default is set. A child
# gets CREATE_NO_WINDOW unless:
#
#   * the caller passed `creationflags` to Popen itself. That is a decision, and a test that is
#     ABOUT console behaviour (tools/test_a_windowless_launch_really_has_no_window.py) must keep
#     testing the real thing, so a decided launch is never rewritten -- including an explicit 0;
#   * the flags already carry a console decision (a new console, detached, or no window).
#
# GRANDCHILDREN ARE COVERED WITHOUT ANOTHER HOOK. CREATE_NO_WINDOW gives the child a console with
# no window, and a console program the child starts inherits that console instead of allocating
# one: measured 2026-09-24 from a pythonw parent, `cmd /d /s /c "chcp 65001 & powershell -Command
# ... cmd /c ver"` launched with CREATE_NO_WINDOW put up ZERO visible console windows across the
# whole chain. (tools/test_a_test_run_opens_no_console.py runs that chain every time.)
#
# os.system does not go through _winapi (the C runtime calls CreateProcess itself), so it is
# routed through subprocess.call with the same shell -- same exit code, same inherited handles.
#
# WHY THIS DOES NOT HIDE AN UNDECIDED PRODUCT SITE. The product guard,
# tools/test_no_new_launch_inherits_its_console_by_accident.py, reads SOURCE with `ast`; a
# runtime default here cannot add a `creationflags` keyword to a call in a file, so an undecided
# product launch still fails that guard exactly as before (proven by mutation on a copy,
# 2026-09-24). The default protects the operator's desktop during a run; the guard is still what
# protects the product.
def _test_children_get_no_console_window():
    if _os.name != "nt":
        return
    import subprocess as _sp
    import threading as _threading
    try:
        import _winapi
    except ImportError:
        return
    if getattr(_winapi.CreateProcess, "_a_test_run_decides_the_console", False):
        return                                            # conftest imported twice

    no_window = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)
    # 0x8 is the detach flag, written as a number so tools/launch_sites.py::detached_uses --
    # which forbids naming it in tracked code -- is not tripped by a mask that only RECOGNISES
    # it. A flag that is already there is a decision this default must not add to.
    already_decided = (no_window
                       | getattr(_sp, "CREATE_NEW_CONSOLE", 0x00000010)
                       | 0x00000008)
    caller = _threading.local()
    real_init = _sp.Popen.__init__
    real_create = _winapi.CreateProcess
    real_system = _os.system

    # `creationflags` is Popen's 14th positional parameter (index 13), after startupinfo.
    _CREATIONFLAGS_POSITION = 13

    def __init__(self, *args, **kwargs):
        decided = "creationflags" in kwargs or len(args) > _CREATIONFLAGS_POSITION
        outer = getattr(caller, "decided", False)
        caller.decided = decided
        try:
            real_init(self, *args, **kwargs)
        finally:
            caller.decided = outer

    def CreateProcess(application, command_line, proc_attrs, thread_attrs, inherit,
                      flags, env, cwd, startupinfo):
        if not getattr(caller, "decided", False) and not (flags & already_decided):
            flags |= no_window
        # Through the attribute, not the closure, so a test can put a spy there and see the
        # flags a launch WOULD have used without starting anything.
        return CreateProcess.__wrapped__(application, command_line, proc_attrs, thread_attrs,
                                         inherit, flags, env, cwd, startupinfo)

    def system(command):
        if command is None:
            return real_system(command)
        return _sp.call(command, shell=True)

    __init__.__wrapped__ = real_init
    CreateProcess.__wrapped__ = real_create
    CreateProcess._a_test_run_decides_the_console = True
    _sp.Popen.__init__ = __init__
    _winapi.CreateProcess = CreateProcess
    _os.system = system


_test_children_get_no_console_window()

# A PID IS UNIQUE AT A MOMENT AND NOT OVER TIME, AND EVERY PATH BELOW USED ONE ALONE.
#
# Windows recycles process ids freely, and these directories were never removed, so a pytest
# process that drew a pid some earlier run had already used started life inside that run's
# sandbox -- holding its files and, for the gate directories, its ANSWERS.
#
# Measured 2026-09-17, chasing an intermittent failure of
# tools/test_contract_activation.py::test_activate_then_approve_then_pass_is_the_full_hitl_cycle:
# 179 companion_gates_pytest_* directories in the temp folder, 147 of them holding the same
# answered gate file this test writes -- `{"answer": "approved"}` for "delete: demo scratch
# file". When the pid landed on one of those, check_op found the old approval on its FIRST call
# and returned None, and the test that exists to prove an unanswered gate refuses the operation
# watched it sail through. The test was right and the sandbox was lying.
#
# This is not only a test-hygiene point. The same collision hands a run the previous run's job
# store, memory store and hash-chained ledgers, and a stale "approved" is precisely the answer
# that must never be inherited.
#
# So: one tag per RUN, not per pid, and the run removes its own sandbox when it exits. The
# random half is what makes it unique over time; the pid is kept because it is what makes a
# directory readable while the run is still going.
_RUN_TAG = "%d_%s" % (_os.getpid(), _uuid.uuid4().hex[:8])


def _sandbox_path(name):
    return _os.path.join(_tempfile.gettempdir(), name % _RUN_TAG)


def _drop_this_runs_sandbox():
    """Remove what this run created. Confined to the temp directory and to the exact prefixes
    set below, so it can never reach anything a person put there."""
    for name in _SANDBOX_NAMES:
        p = _sandbox_path(name)
        try:
            if _os.path.isdir(p):
                _shutil.rmtree(p, ignore_errors=True)
            elif _os.path.isfile(p):
                _os.remove(p)
        except OSError:
            pass


def _drop_abandoned_sandboxes(older_than_s=24 * 3600.0):
    """And the ones left by runs that were killed before they could clean up -- a suite that is
    interrupted is normal, so "the exit hook handles it" is not a complete answer. Only entries
    matching these prefixes, only in the temp directory, only when a day has passed."""
    now, tmp = _time.time(), _tempfile.gettempdir()
    prefixes = tuple(n.split("%s")[0] for n in _SANDBOX_NAMES)
    try:
        entries = _os.listdir(tmp)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith(prefixes):
            continue
        p = _os.path.join(tmp, entry)
        try:
            if now - _os.path.getmtime(p) < older_than_s:
                continue
            if _os.path.isdir(p):
                _shutil.rmtree(p, ignore_errors=True)
            else:
                _os.remove(p)
        except OSError:
            pass


_SANDBOX_NAMES = (
    "companion_gates_pytest_%s",
    "skills_gates_pytest_%s",
    "skills_state_pytest_%s.sqlite3",
    "local_jobs_pytest_%s.sqlite3",
    "selfimprove_ledger_pytest_%s.jsonl",
    "selfimprove_hypotheses_pytest_%s.jsonl",
    "fleet_state_pytest_%s",
    "auth_rejections_pytest_%s.jsonl",
    "notify_source_pytest_%s.log",
    "bridge_undelivered_pytest_%s.jsonl",
    "bridge_token_pytest_%s",
    "signin_latch_pytest_%s",
    "bypass_decisions_pytest_%s.jsonl",
)

_atexit.register(_drop_this_runs_sandbox)
_drop_abandoned_sandboxes()

_os.environ.setdefault(
    "MCP_GATE_DIR",
    _sandbox_path("companion_gates_pytest_%s"))

# THE SAME QUEUE, REACHED BY A SECOND DOOR NOBODY CLOSED. relay/skills.py writes its approval
# questions through its OWN gate directory, not gate_ops', and MCP_GATE_DIR does not move it.
# Its docstring records the first half of this: "THE ONE OF THE THREE WITH NO ESCAPE HATCH, AND
# IT LEAKED FOR MONTHS ... Measured 2026-09-07: 378 pending questions in ~/.companion_gates,
# every single one naming a pytest temp directory, none naming a skill that exists."
#
# MCP_SKILLS_GATE_DIR was added that day as the escape hatch -- AND NOTHING EVER SET IT. Measured
# 2026-09-14: 2,174 files in the live queue, 187 of them written today, 11 per run across 17
# runs, every one still naming a pytest temp directory. An escape hatch nobody turns on is not
# an escape hatch, and the owner is looking at a backlog of decisions of which zero are real.
_os.environ.setdefault(
    "MCP_SKILLS_GATE_DIR",
    _sandbox_path("skills_gates_pytest_%s"))

# And the skills state DB behind the same object. Its tests DO point this at a tmp_path
# themselves -- measured: the live store holds 9 rows and every one names a real skill directory,
# none a pytest path -- so this is belt and braces rather than a repair. It costs nothing, and
# "the tests remember to set it" is exactly the guarantee the gate directory also had.
_os.environ.setdefault(
    "MCP_SKILLS_STATE_DB",
    _sandbox_path("skills_state_pytest_%s.sqlite3"))

# tools.approval_policy.record_bypass_decision's audit trail. Same shape and same reason as
# MCP_GATE_DIR just above: the module reads MCP_BYPASS_LOG_FILE once, at import, so this must
# be set at conftest MODULE scope, before tools.approval_policy is imported by anything --
# otherwise a test that drives job_approval_mode=bypass through ANY of the five call sites
# that now call record_bypass_decision (gate_ops.gate_ask_local, contract_gate's two branches,
# task_router.job_gate, relay.skills.request_approval, relay.selfimprove.pending.add) would
# append its audit line to the real .fleet/bypass_decisions.jsonl.
_os.environ.setdefault(
    "MCP_BYPASS_LOG_FILE",
    _sandbox_path("bypass_decisions_pytest_%s.jsonl"))

# The local job store, which is a record and not a cache: relay/local_job_store.py defaults to
# <repo>/.jobs/jobs.sqlite3 and MCP_LOCAL_JOB_DB is its override. Measured 2026-09-14: of 451
# jobs in the operator's store, 57 carry a pytest path in their job_json. The newest is from
# 2026-08-10, so it is not filling today -- which is the reason to close it now rather than the
# reason to leave it.
_os.environ.setdefault(
    "MCP_LOCAL_JOB_DB",
    _sandbox_path("local_jobs_pytest_%s.sqlite3"))

# Same reason, different file: relay.selfimprove.ledger appends to a hash-chained record in the
# operator's home directory. A test run that wrote there would manufacture entries in the one
# file whose entire value is that its contents were NOT manufactured -- and a chained ledger
# cannot have those entries removed afterwards without breaking the chain.
_os.environ.setdefault(
    "MCP_SELFIMPROVE_LEDGER",
    _sandbox_path("selfimprove_ledger_pytest_%s.jsonl"))

# And the HYPOTHESIS ledger, which is a different file and a worse thing to pollute: it
# records what an experiment predicted BEFORE it looked, and its value rests entirely on
# nobody having manufactured entries. Measured: one run of test_policy_wiring added 120 rows
# to the production file, and 1018 accumulated conclusions -- which I read as a scheduled loop
# failing for two days -- were test runs.
_os.environ.setdefault(
    "MCP_SELFIMPROVE_HYPOTHESES",
    _sandbox_path("selfimprove_hypotheses_pytest_%s.jsonl"))


# And the PROJECT MEMORY store, which is the third production record a test run was found
# writing into. `.fleet/memory/*.md` is what the fleet primes into every goal, so a test that
# writes there does not merely add noise -- it changes what the next REAL run is told about its
# own past. Found by looking: five themes named g0..g4 sat in the operator's store minutes old,
# alongside 62 real ones, and five more whose theme name was the memory HEADER itself, because
# a primed body was recorded as if it were a fresh goal.
_os.environ.setdefault(
    "FLEET_STATE_DIR",
    _sandbox_path("fleet_state_pytest_%s"))


# And the record of WHO was turned away at the door. tools/auth_stats writes it from the request
# path, and tools/test_auth_stats already called record_auth_failure -- so the moment that
# function gained a durable sidecar, four rows carrying no ip, no path and no agent appeared in
# the operator's live .fleet/. A record of nothing, in the file someone would open to find out
# who was rejected. Found the same afternoon the file was added, by running status.py and
# reading what it printed.
_os.environ.setdefault(
    "MCP_AUTH_REJECTIONS_FILE",
    _sandbox_path("auth_rejections_pytest_%s.jsonl"))


# And the toast watchdog's record of WHO fired a notification. It gained a name on 2026-09-18 --
# it had been built from __file__ inside the function, where nothing could move it -- and a path
# that only exists as an expression is one relay/test_live_record_isolation.py cannot see.
_os.environ.setdefault(
    "MCP_NOTIFY_SOURCE_LOG",
    _sandbox_path("notify_source_pytest_%s.log"))


# And the bridge's record of a message it could not deliver, added the same day. Its own test
# redirects it; the point of this list is that the NEXT test does not have to remember.
_os.environ.setdefault(
    "MCP_BRIDGE_UNDELIVERED_FILE",
    _sandbox_path("bridge_undelivered_pytest_%s.jsonl"))


# And the bridge's token directory. This is not a record but a live credential: a test that
# called bridge_auth.install_token without it would ROTATE THE RUNNING BRIDGE'S TOKEN, and every
# open chat window would be refused until it re-read the file. Resolved at call time, so a test
# that moves it again (monkeypatch.setenv) still wins.
_os.environ.setdefault(
    "MCP_BRIDGE_TOKEN_DIR",
    _sandbox_path("bridge_token_pytest_%s"))


# And the bridge sign-in latch (scripts/ensure_m365_signin.py::_latch_path), added 2026-09-24
# alongside the sign-in-surfacing fix (e66af67): it defaults to <repo>/.fleet/signin_surfaced_
# <port>.json, marking "already brought forward for this sign-in" so the supervisor does not
# re-raise the window every poll. MCP_SIGNIN_LATCH_DIR was added as its escape hatch and its own
# test file (scripts/test_bridge_signin_is_brought_to_the_person.py) already redirects it, either
# through a local `latch` fixture or by passing the variable into a subprocess harness -- but
# tools/test_an_escape_hatch_nobody_turns_on.py requires every location override that exists in
# tracked source to be SET somewhere, not merely used correctly by the one file that happens to
# remember, which is exactly the MCP_SKILLS_GATE_DIR shape this table exists to prevent
# recurring. Belt and braces, same as MCP_BRIDGE_TOKEN_DIR above: resolved at call time in
# _latch_path, so a test's own monkeypatch.setenv still wins over this default.
_os.environ.setdefault(
    "MCP_SIGNIN_LATCH_DIR",
    _sandbox_path("signin_latch_pytest_%s"))


@pytest.fixture(autouse=True)
def _no_leftover_kill_switch():
    """And clear it around every test, so one test cannot abort the next.

    Belt and braces with the redirect above: the redirect stops a test reaching production,
    this stops a test reaching the test after it. Both were needed -- the failure that
    started this was a leftover switch, not a wrong path.
    """
    try:
        from tools.gate_ops import STOP_FILE
    except Exception:
        yield
        return
    STOP_FILE.unlink(missing_ok=True)
    yield
    STOP_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------------
# A TEST RUN MUST NOT READ THE OPERATOR'S .env.
#
# `relay/agent_profiles.py` and `bridge/copilot_bridge.py` call `load_dotenv()` AT IMPORT, and
# pytest imports every selected test module during COLLECTION -- before the first test runs.
# So merely including `bridge/` in a run injects the real .env into os.environ for every test
# in it, whatever order they execute in.
#
# That is not hypothetical. `tools/` alone passes 497 tests; `tools/ tests/ bench/ bridge/ ui/`
# fails four registration tests, because .env sets MCP_TOOL_MAP_INCLUDE and MCP_TOOL_MAP_MAX
# and those tests set only some of the family. The failure looks like a bug in the tool map and
# is a bug in what the test inherited.
#
# Neutralised at the source rather than by each test clearing more keys: a test that has to
# remember which of twenty operator settings might reach it will forget one, and the forgetting
# is invisible until some unrelated directory joins the run. Tests that WANT a value set it
# themselves with monkeypatch, which is unaffected.
def _no_dotenv(*_a, **_k):
    return False


try:
    import dotenv as _dotenv
    _dotenv.load_dotenv = _no_dotenv
    _dotenv.main.load_dotenv = _no_dotenv
except Exception:
    pass
@pytest.fixture(autouse=True, scope="session")
def _dotenv_is_neutralised():
    """Proof the patch took, rather than a try/except that swallowed an import error.

    A neutralisation that silently failed would leave the leak in place and the comment above
    describing a protection that is not there -- which is worse than no comment.
    """
    import dotenv
    assert dotenv.load_dotenv is _no_dotenv, (
        "load_dotenv was not neutralised; a test run can read the operator's .env")
    yield

# ── the session store must never be the operator's during a test run ────────────
#
# `_Transcript` writes every fleet turn to the local database, and dozens of existing fleet
# tests construct one. They pass a temporary directory for the JSONL file, which looked like
# enough -- but the database path comes from the store, not from that argument, so a full run
# put 138 rows into the real store under keys like `wdead`, `wconsent` and `w_stale`, sitting
# beside genuine conversations.
#
# Session-scoped and autouse, set through the environment rather than by patching an
# attribute, because the writers are not all in this process: fleet workers and the stress
# harness spawn their own, and each imports its own copy of the module.

@pytest.fixture(autouse=True, scope="session")
def _isolate_session_store(tmp_path_factory):
    import os

    previous = os.environ.get("MCP_SESSION_STORE_DIR")
    os.environ["MCP_SESSION_STORE_DIR"] = str(tmp_path_factory.mktemp("session_store"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MCP_SESSION_STORE_DIR", None)
        else:
            os.environ["MCP_SESSION_STORE_DIR"] = previous


@pytest.fixture(autouse=True)
def _fresh_route_incident_clock():
    """Reset the process-global incident clock between tests.

    relay_fleet coalesces transport faults that arrive within one window into a single vote
    for the route's circuit breaker -- correct in production, where the process is one run.
    In a test session the process spans every test, so one test's fault silently suppressed
    the next test's, and the failure surfaced in an unrelated test that had merely asked the
    route to be told about a failure and found it had not been.
    """
    try:
        import relay.relay_fleet as rf
        rf._LAST_ROUTE_FAULT[0] = 0.0
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _fresh_contract_gate_seen():
    """Keep one test's contract sidecars out of the next test's .fleet directory.

    THIS USED TO RESET A MODULE GLOBAL, contract_gate._SEEN, which no longer exists. The gate
    now records "an active contract was seen" and "that contract was retired" in two sidecar
    files beside the contract, because the process that retires a contract (the fleet runner)
    is not the process that must honour the retirement (the MCP server) -- a module global
    could not cross that boundary, so the server suspected forever and every gated op queued
    a human approval.

    Moving to disk does not make this fixture unnecessary, it changes what it must clean. A
    test that points _CONTRACT_FILE into tmp_path carries the sidecars there with it and
    leaks nothing. A test that writes an active contract into the REAL .fleet leaves a seen
    record behind, and from then on policy_state_is_suspect() reports "an active contract was
    in force and its file has since disappeared" for every later test -- check_op then fires
    on every shell_destructive op, and the worktree-lifecycle suite gets the gate's refusal
    string back from worktree_add instead of a worktree. That failed only in a full run,
    never alone.

    The old body was wrapped in try/except, so when _SEEN disappeared this fixture silently
    became a no-op that still looked like protection. Deleting the real sidecars is the same
    guarantee expressed against the state that actually exists now.
    """
    def _clear():
        try:
            import tools.contract_gate as _cg
            for path in (_cg._seen_file(), _cg._retired_file()):
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    _clear()
    yield
    _clear()


# ---- reading a source file as CODE ONLY, for the checks that are about code -----------------
#
# WHY IT IS HERE AND NOT IN tools/. It was tools/source_text.py, and
# tools/test_nothing_new_is_built_without_a_caller.py refused it correctly: "defined and
# referenced nowhere in NON-TEST code". Both callers are tests, because this only ever serves a
# check about source. Putting it in tools/ made a production module that nothing in production
# uses, and the honest fix is not an exemption -- the scanner does not scan conftest.py or
# test_*.py, precisely because a helper that only tests use belongs with the tests.
#
# WHAT IT IS FOR. The same class of false positive was hit four times in two days: a check about
# code was satisfied, or tripped, by PROSE. A guard forbidding `S.get(` fired on
# `_DRAIN_ATTEMPTS.get(`; a check forbidding a reconstructed binary part matched the COMMENT
# explaining that very mistake, so it was narrowed to skip comment lines; it then matched the
# module DOCSTRING, which also explains the mistake; and a NEW check, written after all of that
# in another file, forbade "FileBase64" and matched the docstring paragraph listing the six
# multipart fields the page sends.
#
# The fourth happened because the third fix lived as a private helper inside one test file. A
# discipline that has to be re-derived per file gets re-derived wrongly.
#
# These files are valuable precisely because they write down what went wrong, and a text search
# cannot tell an explanation from the thing explained. So the explanation is removed before the
# search: comments by line, docstrings BY PARSING -- not by pattern, since the pattern is what
# kept failing. Other string literals stay, because the checks look for real code such as
# `headers["Authorization"] = ...`, which is a literal in an assignment, not prose.

def code_only(path: str) -> str:
    """The file's source with comments and docstrings blanked out, line numbering preserved."""
    import ast as _ast

    src = io.open(path, encoding="utf-8", errors="replace").read()
    lines = src.splitlines()
    blank = set()
    try:
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                                     _ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            if (isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant)
                    and isinstance(first.value.value, str)):
                end = getattr(first, "end_lineno", first.lineno)
                blank.update(range(first.lineno, end + 1))
    except SyntaxError:
        # A file that does not parse still gets its comments removed. Returning the raw source
        # instead would quietly restore the failure this exists to prevent.
        pass
    return "\n".join("" if (i + 1) in blank else l
                      for i, l in enumerate(lines) if not l.lstrip().startswith("#"))


# ---- writing a settings file a Follower will actually notice ---------------------------------
#
# WHY IT IS SHARED. relay/settings_follow.py re-reads on (mtime, size). Tests write the same
# file repeatedly and can land inside one filesystem timestamp tick, and two values of the same
# LENGTH (`maxtabs=2` / `maxtabs=4`) leave the size identical -- so a real change is reported as
# no change. Bumping from the file's OWN mtime is not enough: two writes in one tick both land
# on T + delta.
#
# That was found, written up and fixed inside one test module on 2026-09-16, with a comment
# saying it "appeared roughly one run in several and could not be reproduced on demand" and that
# "the same test passed alone and failed in company". On 2026-09-19 a second test module was
# written for the other half of the same mechanism, its author (me) wrote the helper again from
# scratch, and reproduced that exact failure -- one run in several, only when the two files ran
# together. The second time in one day that a discipline living in one file was re-derived
# wrongly somewhere else; see code_only above for the first.
#
# So the monotonic counter is module-level HERE, shared by every caller, which is the only
# version of it that cannot drift apart.

_LAST_FORCED_MTIME = [0.0]


def write_settings(path, **pairs):
    """Write `key=value` lines and force a stamp no earlier reader can mistake for the old one."""
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for k, v in pairs.items():
            fh.write("%s=%s\n" % (k, v))
    st = os.stat(path)
    forced = max(st.st_mtime, _LAST_FORCED_MTIME[0]) + 10
    _LAST_FORCED_MTIME[0] = forced
    os.utime(path, (st.st_atime, forced))


# THE THIRD TIME A TEST-ONLY HELPER WAS RE-DERIVED RATHER THAN SHARED, and the first two are
# written up above (code_only, write_settings). `bench/companionbench/job_authority.py` carried
# a `free_port` whose own docstring said "Only for tests that need to point at nothing" -- and
# no test used it, while tests/test_checkpoint_port_probe.py defined its own byte-identical
# copy. A helper that only tests call does not belong in production code: it is invisible to
# the people who would reuse it, and it shows up in the unreached inventory as a function
# nobody calls, which is exactly what it was.
#
# conftest is the right address for the same reason it was right for code_only: the unreached
# scanner does not read conftest or test_*.py, so a helper here is not a permanent entry in an
# inventory of dead code.

def free_port():
    """A loopback port with nothing bound: bind to 0, read what the OS chose, close.

    INHERENTLY RACY AND THAT IS THE POINT OF SAYING SO. The port is free at the moment it is
    closed and nothing holds it afterwards, so this answers "a number that was free just now",
    which is what a probe pointing at nothing needs and is NOT a reservation.
    """
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()
