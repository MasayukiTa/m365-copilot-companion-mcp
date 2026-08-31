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


def test_the_client_capabilities_this_module_uses_exist():
    import inspect
    from fastmcp import Context
    for name in ("sample", "elicit"):
        fn = getattr(Context, name, None)
        assert fn is not None, "fastmcp.Context has no %s" % name
        assert inspect.iscoroutinefunction(fn), (
            "%s stopped being async; the thread bridge in judge_backend assumes it is" % name)


def test_sample_still_takes_a_separate_system_prompt():
    """The whole anti-injection property depends on the instructions and the payload
    travelling in different fields. If this parameter goes away, concatenating them back
    together is NOT the fix."""
    import inspect
    from fastmcp import Context
    assert "system_prompt" in inspect.signature(Context.sample).parameters


# ── choosing a backend ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expect_none", [
    (None, True), ("", True), ("none", True), ("off", True),
    ("nonsense", True),          # asked for a judge we do not have -> no judge, not a bypass
    ("sampling", False),
])
def test_backend_selection(monkeypatch, val, expect_none):
    if val is None:
        monkeypatch.delenv(B.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(B.BACKEND_ENV, val)
    got = B.get()
    assert (got is None) is expect_none


def test_the_default_is_no_judge():
    """Not because no judge is good, but because a judge that has never been measured must not
    be switched on by an upgrade."""
    import os
    assert (os.environ.get(B.BACKEND_ENV) or "none") in ("none", "off", "", "sampling")


# ── with nobody to ask ────────────────────────────────────────────────────────────────────

def test_outside_a_request_there_is_no_context():
    assert B._context() is None


def test_sampling_outside_a_request_raises_rather_than_returning_something():
    """It must RAISE. A backend that returned "" here would reach parse_verdict, which would
    also refuse -- but through a path that reads as "the model said nothing" rather than
    "there was no model", and those need different fixes."""
    with pytest.raises(B.JudgeTransportError):
        B.sampling_judge('{"pending_command":"rm -rf /"}')


def test_a_raising_backend_becomes_require_human_not_allow():
    out = J.judge_command({}, B.sampling_judge)
    assert out["decision"] == J.REQUIRE_HUMAN
    assert out["source"] == "unavailable"


def test_no_human_reachable_is_not_an_approval():
    assert B.human_available() is False
    assert B.ask_human("may I?") is None


# ── what the client declared ──────────────────────────────────────────────────────────────

def test_the_capability_types_this_module_names_are_real():
    """Same guard as the API-name tests above, for the same reason: a wrong name here is
    caught by an `except Exception` and reads as "the client cannot do it"."""
    from mcp.types import ClientCapabilities, ElicitationCapability, SamplingCapability
    assert ClientCapabilities(sampling=SamplingCapability()).sampling is not None
    assert ClientCapabilities(elicitation=ElicitationCapability()).elicitation is not None


def test_the_session_check_exists():
    from mcp.server.session import ServerSession
    assert hasattr(ServerSession, "check_client_capability")


@pytest.mark.parametrize("fn", [B.sampling_supported, B.elicitation_supported])
def test_outside_a_request_nothing_is_supported(fn):
    assert fn() is False


def test_a_session_that_says_no_is_believed(monkeypatch):
    class _S:
        def check_client_capability(self, _c):
            return False
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.sampling_supported() is False
    assert B.human_available() is False


def test_a_session_that_says_yes_is_believed(monkeypatch):
    class _S:
        def check_client_capability(self, _c):
            return True
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.sampling_supported() is True
    assert B.human_available() is True


def test_a_session_that_raises_is_not_taken_as_yes(monkeypatch):
    """FAIL CLOSED. human_available() feeds outcome_blocks_execution, where True means
    REQUIRE_HUMAN stops blocking -- so an unknown answer read as "yes" turns "ask a person"
    into "carry on" on exactly the deployments with no person."""
    class _S:
        def check_client_capability(self, _c):
            raise RuntimeError("older client")
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": _S()})())
    assert B.sampling_supported() is False
    assert B.elicitation_supported() is False
    assert B.human_available() is False


def test_human_available_is_about_the_capability_not_about_being_in_a_request(monkeypatch):
    """The first version returned True whenever a context existed. A client can call a tool
    and have no way to show its user anything; those are different questions."""
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": None})())
    assert B._context() is not None
    assert B.human_available() is False


def test_sampling_refuses_immediately_when_the_client_never_declared_it(monkeypatch):
    """Otherwise every judged command pays the full timeout to learn something the handshake
    already said."""
    monkeypatch.setattr(B, "_context", lambda: type("C", (), {"session": None})())
    monkeypatch.setattr(B, "sampling_supported", lambda: False)
    with pytest.raises(B.JudgeTransportError) as exc:
        B.sampling_judge("{}")
    assert "sampling" in str(exc.value)


def test_availability_separates_never_configured_from_cannot_run(monkeypatch):
    monkeypatch.delenv(B.BACKEND_ENV, raising=False)
    a = B.availability()
    assert a["configured"] is False and a["backend"] == "none"

    monkeypatch.setenv(B.BACKEND_ENV, "sampling")
    b = B.availability()
    assert b["configured"] is True
    assert b["client_sampling"] is False, "no request context -> the client can do nothing"
    # The two states must be distinguishable, which is the whole point of the field.
    assert a != b


# ── reading the client's answer ───────────────────────────────────────────────────────────

class _Text:
    def __init__(self, t):
        self.text = t


class _Nested:
    def __init__(self, t):
        self.content = _Text(t)


@pytest.mark.parametrize("obj,want", [
    (_Text('{"decision":"ALLOW"}'), '{"decision":"ALLOW"}'),
    (_Nested('{"decision":"ALLOW"}'), '{"decision":"ALLOW"}'),
    ("plain string", "plain string"),
])
def test_the_answer_text_is_found_whatever_shape_it_arrives_in(obj, want):
    assert B._text_of(obj) == want


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


def test_an_elicitation_that_throws_is_not_an_approval(monkeypatch):
    monkeypatch.setattr(B, "_context", lambda: object())

    def _boom(fn, *a, **k):
        raise RuntimeError("client does not support elicitation")
    monkeypatch.setattr(B, "_run_async", _boom)
    assert B.ask_human("may I?") is None
