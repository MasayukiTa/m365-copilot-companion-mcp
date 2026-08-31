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
ONLY_IF_ALREADY_IMPORTED = frozenset({"bridge.copilot_bridge", "bridge.session_store"})

LIVE_RECORD_REDIRECTS = {
    "tools.lock_state": {"_LOG_FILE": "lock_refusals.jsonl",
                         "_STATE_FILE": "lock_state.json"},
    "relay.socket_route": {"DEFAULT_LOG": "socket_route.jsonl"},
    "relay.selfimprove.pending": {"QUEUE_PATH": "pending_decisions.jsonl"},
    "relay.selfimprove.record_summary": {"CACHE_PATH": "record_summaries.json"},
    "relay.capture_status": {"STATUS_PATH": "capture_status.json"},
    "relay.selfimprove.ledger": {"DEFAULT_PATH": "hypotheses.jsonl"},
    "relay.selfimprove.compare": {"QUEUE_PATH": "compare_queue.jsonl",
                                  "RESULTS_PATH": "compare_results.jsonl"},
    "relay.selfimprove.runtime_config": {"ACTIVE_PATH": "active_manifest.json"},
    "relay.settle_replay": {"DEFAULT_TRACE": "settle_trace.jsonl"},
    "relay.task_router": {"APPROVED_JOBS_FILE": "approved_jobs.json",
                          "TASKS": "tasks.jsonl"},
    "tools.auth_stats": {"_STATS_FILE": "auth_stats.json"},
    "tools.skill_ops": {"_SKILL_USE_LOG": "skill_use.jsonl"},
    "relay.mechanism_telemetry": {"LOG": "mechanisms.jsonl"},
    "relay.ownership": {"LEDGER": "ownership.jsonl"},
    "relay.edge_reconnect": {"CONN_URL_CACHE": "conn_manager_url.txt"},
    "relay.selfimprove.branches": {"DEFAULT_PATH": "branches.jsonl"},
    "relay.copilot_autopilot_relay": {"_SEND_STAGE_PATH": "send_stage.jsonl",
                                      "_SETTLE_TRACE_PATH": "settle_trace_car.jsonl"},
    # A DIRECTORY OF CAPTURED TOKENS AND REQUEST TEMPLATES. Not a log -- the one place in this
    # list where a stray test write would touch live credentials material.
    "relay.profile_token": {"TEMPLATE_DIR": "templates"},
    "bridge.copilot_bridge": {"DELETE_LOG": "delete_log.jsonl",
                              "FLEET_CONVS_PATH": "fleet_convs.json",
                              "RECYCLE_SAMPLES_PATH": "recycle_samples.jsonl",
                              "_SETTLE_RESET_TRACE_PATH": "settle_reset_trace.jsonl"},
    "bridge.session_store": {"SESS_DIR": "sessions"},
    "tools.tool_probe": {"_PROBE_FILE": "tool_probe.json",
                         "PROBE_FAILURE_JOURNAL": "tool_probe_failures.jsonl",
                         "_INBOUND_PATH": "probe_inbound.json"},
}

#: Constants that build a .fleet path but are NOT redirected, each with the reason. Being on
#: this list is a claim that a test writing there is harmless -- so it is short, and each line
#: has to be defensible.
DELIBERATELY_NOT_REDIRECTED = {
    ("tools.contract_gate", "_FLEET_DIR"):
        "a directory, not a record; the gate's own files are redirected by the tests that "
        "write them and the contract file is already per-test",
    ("tools.folder_policy", "POLICY_FILE"):
        "read by the policy gate and written only by the operator's console; a test that "
        "wrote it would be testing the console, which none do",
    ("relay.selfimprove.loop", "SWEDIR"):
        "a benchmark working directory, not an operator record",
    ("relay.selfimprove.quality_loop", "SWEDIR"):
        "a benchmark working directory, not an operator record",
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

_os.environ.setdefault(
    "MCP_GATE_DIR",
    _os.path.join(_tempfile.gettempdir(), "companion_gates_pytest_%d" % _os.getpid()))

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
