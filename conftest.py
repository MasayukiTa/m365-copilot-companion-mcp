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
    try:
        from pathlib import Path

        import tools.lock_state as lock_state
        monkeypatch.setattr(lock_state, "_LOG_FILE", Path(str(base / "lock_refusals.jsonl")),
                            raising=False)
        monkeypatch.setattr(lock_state, "_STATE_FILE", Path(str(base / "lock_state.json")),
                            raising=False)
    except Exception:
        pass

    try:
        import relay.socket_route as socket_route
        monkeypatch.setattr(socket_route, "DEFAULT_LOG", str(base / "socket_route.jsonl"),
                            raising=False)
    except Exception:
        pass

    # THE NEXT ONE, CAUGHT BY THE LAYER THAT EXISTS FOR IT. Wiring frozen.py's refusal to the
    # pending queue gave a test that runs the real CLI a route into the operator's live queue:
    # a proposal about manifest.py, reason "routine", appeared among the real decisions within
    # minutes. That is the fourth time a test has written to a live record here, which is the
    # whole reason this fixture exists -- so the entry goes in here rather than in the one
    # test that happened to trip it.
    try:
        import relay.selfimprove.pending as pending
        monkeypatch.setattr(pending, "QUEUE_PATH", str(base / "pending_decisions.jsonl"),
                            raising=False)
    except Exception:
        pass

    # Derived rather than a record, but still the operator's file: a test that regenerated it
    # would replace summaries that cost real model calls, and the failure would look like
    # nothing at all -- the screen simply falls back to raw reasons.
    try:
        import relay.selfimprove.record_summary as record_summary
        monkeypatch.setattr(record_summary, "CACHE_PATH",
                            str(base / "record_summaries.json"), raising=False)
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
