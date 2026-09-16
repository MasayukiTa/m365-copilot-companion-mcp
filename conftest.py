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
import os as _os
import tempfile as _tempfile

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

_os.environ.setdefault(
    "MCP_GATE_DIR",
    _os.path.join(_tempfile.gettempdir(), "companion_gates_pytest_%d" % _os.getpid()))

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
    _os.path.join(_tempfile.gettempdir(), "skills_gates_pytest_%d" % _os.getpid()))

# And the skills state DB behind the same object. Its tests DO point this at a tmp_path
# themselves -- measured: the live store holds 9 rows and every one names a real skill directory,
# none a pytest path -- so this is belt and braces rather than a repair. It costs nothing, and
# "the tests remember to set it" is exactly the guarantee the gate directory also had.
_os.environ.setdefault(
    "MCP_SKILLS_STATE_DB",
    _os.path.join(_tempfile.gettempdir(), "skills_state_pytest_%d.sqlite3" % _os.getpid()))

# The local job store, which is a record and not a cache: relay/local_job_store.py defaults to
# <repo>/.jobs/jobs.sqlite3 and MCP_LOCAL_JOB_DB is its override. Measured 2026-09-14: of 451
# jobs in the operator's store, 57 carry a pytest path in their job_json. The newest is from
# 2026-08-10, so it is not filling today -- which is the reason to close it now rather than the
# reason to leave it.
_os.environ.setdefault(
    "MCP_LOCAL_JOB_DB",
    _os.path.join(_tempfile.gettempdir(), "local_jobs_pytest_%d.sqlite3" % _os.getpid()))

# Same reason, different file: relay.selfimprove.ledger appends to a hash-chained record in the
# operator's home directory. A test run that wrote there would manufacture entries in the one
# file whose entire value is that its contents were NOT manufactured -- and a chained ledger
# cannot have those entries removed afterwards without breaking the chain.
_os.environ.setdefault(
    "MCP_SELFIMPROVE_LEDGER",
    _os.path.join(_tempfile.gettempdir(), "selfimprove_ledger_pytest_%d.jsonl" % _os.getpid()))

# And the HYPOTHESIS ledger, which is a different file and a worse thing to pollute: it
# records what an experiment predicted BEFORE it looked, and its value rests entirely on
# nobody having manufactured entries. Measured: one run of test_policy_wiring added 120 rows
# to the production file, and 1018 accumulated conclusions -- which I read as a scheduled loop
# failing for two days -- were test runs.
_os.environ.setdefault(
    "MCP_SELFIMPROVE_HYPOTHESES",
    _os.path.join(_tempfile.gettempdir(), "selfimprove_hypotheses_pytest_%d.jsonl"
                  % _os.getpid()))


# And the PROJECT MEMORY store, which is the third production record a test run was found
# writing into. `.fleet/memory/*.md` is what the fleet primes into every goal, so a test that
# writes there does not merely add noise -- it changes what the next REAL run is told about its
# own past. Found by looking: five themes named g0..g4 sat in the operator's store minutes old,
# alongside 62 real ones, and five more whose theme name was the memory HEADER itself, because
# a primed body was recorded as if it were a fresh goal.
_os.environ.setdefault(
    "FLEET_STATE_DIR",
    _os.path.join(_tempfile.gettempdir(), "fleet_state_pytest_%d" % _os.getpid()))


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
