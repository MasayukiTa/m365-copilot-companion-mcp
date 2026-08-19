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
