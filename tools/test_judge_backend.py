"""Who gets asked, what happens when nobody can be, and whether the names are real.

THE LAST POINT IS NOT A JOKE. The first version of judge_backend.py called
`chathub.default_connect` and `profile_token.supplier()`. Neither exists. Three sets of
invented API names reached files in a single day, each caught by something other than me
reading the module, and the failure is quiet here: a wrong name raises, judge_command turns the
raise into REQUIRE_HUMAN, and in shadow that is one more indistinguishable line in a log full
of REQUIRE_HUMAN. So the imports this module depends on are asserted directly.
"""
import pytest

from tools import command_judge as J
from tools import judge_backend as B


# ── the names are real ────────────────────────────────────────────────────────────────────

def test_the_context_accessor_exists_where_this_module_expects_it():
    from fastmcp.server.dependencies import get_context
    assert callable(get_context)


def test_the_client_capability_this_module_uses_exists():
    import inspect
    from fastmcp import Context
    fn = getattr(Context, "elicit", None)
    assert fn is not None, "fastmcp.Context has no elicit"
    assert inspect.iscoroutinefunction(fn), (
        "elicit stopped being async; the thread bridge in judge_backend assumes it is")


def test_the_sampling_backend_is_gone():
    """fastmcp 4 removed Context.sample and production never ran sampling. Nothing in this
    module may still offer it, or a caller could pick a judge that cannot answer."""
    for name in ("sampling_judge", "sampling_judge_async", "sampling_supported", "_text_of"):
        assert not hasattr(B, name), "%s should have been removed with Context.sample" % name


# ── choosing a backend ────────────────────────────────────────────────────────────────────

@pytest.fixture
def _telemetry_in_tmp(monkeypatch, tmp_path):
    """The one-time sampling warning writes a mechanism row; keep it out of the real .fleet."""
    from relay import mechanism_telemetry as mt
    monkeypatch.setattr(mt, "LOG", str(tmp_path / "mechanisms.jsonl"))
    monkeypatch.setattr(B, "_SAMPLING_WARNED", False)
    return mt


@pytest.mark.parametrize("val", [None, "", "none", "off",
                                 "nonsense",   # asked for a judge we do not have -> no judge, not a bypass
                                 "sampling"])  # removed backend: degrades to no judge
def test_backend_selection_gives_no_judge(monkeypatch, _telemetry_in_tmp, val):
    if val is None:
        monkeypatch.delenv(B.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(B.BACKEND_ENV, val)
    assert B.get() is None


def test_bridge_is_still_selectable(monkeypatch):
    monkeypatch.setenv(B.BACKEND_ENV, "bridge")
    assert B.get() is B.bridge_judge


def test_a_configured_sampling_backend_warns_once_and_degrades_to_none(
        monkeypatch, _telemetry_in_tmp, caplog):
    """Safe degradation: still no judge (so REQUIRE_HUMAN, recorded in shadow), with exactly one
    warning log line and one mechanism row however many commands are judged."""
    import json
    import logging
    monkeypatch.setenv(B.BACKEND_ENV, "sampling")
    with caplog.at_level(logging.WARNING, logger=B.__name__):
        assert B.get() is None
        assert B.get() is None
        assert B.get() is None
    assert len([r for r in caplog.records if "sampling" in r.getMessage()]) == 1
    rows = [json.loads(line) for line in
            open(_telemetry_in_tmp.LOG, encoding="utf-8").read().splitlines()]
    assert [r["mechanism"] for r in rows] == ["judge_backend_sampling_removed"]
    assert "judge_backend_sampling_removed" in _telemetry_in_tmp.MECHANISMS
    out = J.judge_command({}, B.get())
    assert out["decision"] == J.REQUIRE_HUMAN
    assert B.availability()["sampling_backend_removed"] is True


def test_the_default_is_no_judge():
    """Not because no judge is good, but because a judge that has never been measured must not
    be switched on by an upgrade."""
    import os
    assert (os.environ.get(B.BACKEND_ENV) or "none") in ("none", "off", "", "sampling")


# ── with nobody to ask ────────────────────────────────────────────────────────────────────

def test_outside_a_request_there_is_no_context():
    assert B._context() is None


def test_bridge_outside_a_reachable_bridge_raises_rather_than_returning_something(monkeypatch):
    """It must RAISE. A backend that returned "" here would reach parse_verdict, which would
    also refuse -- but through a path that reads as "the model said nothing" rather than
    "there was no model", and those need different fixes."""
    monkeypatch.setenv(B.BRIDGE_PORT_ENV, "1")      # nothing listens there
    with pytest.raises(B.JudgeTransportError):
        B.bridge_judge('{"pending_command":"rm -rf /"}')


def test_a_raising_backend_becomes_require_human_not_allow():
    def _boom(_req):
        raise B.JudgeTransportError("nobody to ask")
    out = J.judge_command({}, _boom)
    assert out["decision"] == J.REQUIRE_HUMAN
    assert out["source"] == "unavailable"


def test_no_human_reachable_is_not_an_approval():
    assert B.human_available() is False
    assert B.ask_human("may I?") is None


# ── what the client declared ──────────────────────────────────────────────────────────────

def test_the_capability_types_this_module_names_are_real():
    """Same guard as the API-name tests above, for the same reason: a wrong name here is
    caught by an `except Exception` and reads as "the client cannot do it"."""
    from mcp.types import ClientCapabilities, ElicitationCapability
    assert ClientCapabilities(elicitation=ElicitationCapability()).elicitation is not None


def test_the_session_check_exists():
    from mcp.server.session import ServerSession
    assert hasattr(ServerSession, "check_client_capability")


def test_outside_a_request_nothing_is_supported():
    assert B.elicitation_supported() is False


def test_a_session_that_says_no_is_believed(monkeypatch):
    class _S:
        def check_client_capability(self, _c):
            return False
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.elicitation_supported() is False
    assert B.human_available() is False


def test_a_session_that_says_yes_is_believed(monkeypatch):
    class _S:
        def check_client_capability(self, _c):
            return True
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.elicitation_supported() is True
    assert B.human_available() is True


def test_a_session_that_raises_is_not_taken_as_yes(monkeypatch):
    """FAIL CLOSED. human_available() feeds outcome_blocks_execution, where True means
    REQUIRE_HUMAN stops blocking -- so an unknown answer read as "yes" turns "ask a person"
    into "carry on" on exactly the deployments with no person."""
    class _S:
        def check_client_capability(self, _c):
            raise RuntimeError("older client")
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.elicitation_supported() is False
    assert B.human_available() is False


def test_human_available_is_about_the_capability_not_about_being_in_a_request(monkeypatch):
    """The first version returned True whenever a context existed. A client can call a tool
    and have no way to show its user anything; those are different questions."""
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": None})())
    assert B._context() is not None
    assert B.human_available() is False


def test_availability_separates_never_configured_from_cannot_run(monkeypatch):
    monkeypatch.delenv(B.BACKEND_ENV, raising=False)
    a = B.availability()
    assert a["configured"] is False and a["backend"] == "none"

    monkeypatch.setenv(B.BACKEND_ENV, "sampling")
    b = B.availability()
    assert b["configured"] is True
    assert b["sampling_backend_removed"] is True, "a stale `sampling` setting must say so"
    assert "client_sampling" not in b
    # The two states must be distinguishable, which is the whole point of the field.
    assert a != b


# ── the human verdict, by result type ─────────────────────────────────────────────────────

class AcceptedElicitation:      # names mirror fastmcp's
    pass


class DeclinedElicitation:
    pass


class CancelledElicitation:
    pass


class SomethingElse:
    pass


@pytest.mark.parametrize("cls,want", [
    (AcceptedElicitation, True),
    (DeclinedElicitation, False),
    (CancelledElicitation, False),
    (SomethingElse, None),      # unrecognised is NOT an approval
])
def test_only_an_acceptance_is_an_approval(monkeypatch, cls, want):
    monkeypatch.setattr(B, "_context", lambda: object())
    monkeypatch.setattr(B, "_run_async", lambda fn, *a, **k: cls())
    assert B.ask_human("may I?") is want


def test_an_accepted_answer_of_false_is_not_an_approval():
    """fastmcp 4 asks the question as a bool confirmation (response_type=None is refused), so an
    accepted `False` must read as a refusal."""
    class AcceptedElicitation:
        def __init__(self, data):
            self.data = data
    assert B._approval_of(AcceptedElicitation(True)) is True
    assert B._approval_of(AcceptedElicitation(False)) is False


def test_the_question_is_asked_with_a_response_type(monkeypatch):
    """ctx.elicit() in fastmcp 4 raises TypeError without one; ask_human would swallow that and
    answer None forever, so pin the argument."""
    seen = {}

    class _Ctx:
        async def elicit(self, message, response_type=None, **kw):
            seen["response_type"] = response_type
            return type("AcceptedElicitation", (), {"data": True})()

    monkeypatch.setattr(B, "_context", lambda: _Ctx())
    monkeypatch.setattr(B, "_run_async", lambda fn, *a, **k: __import__("asyncio").run(fn()))
    assert B.ask_human("may I?") is True
    assert seen["response_type"] is bool


def test_an_elicitation_that_throws_is_not_an_approval(monkeypatch):
    monkeypatch.setattr(B, "_context", lambda: object())

    def _boom(fn, *a, **k):
        raise RuntimeError("client does not support elicitation")
    monkeypatch.setattr(B, "_run_async", _boom)
    assert B.ask_human("may I?") is None
