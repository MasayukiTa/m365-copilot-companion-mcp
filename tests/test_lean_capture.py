"""Blocking the pixels on the capture page must never cost a turn.

The capture page is the socket route's last browser cost: one page, once per token lifetime,
on which a real turn runs. A browser with such a page open measured 697 MB against 341 MB
without, across 4,549 samples. Blocking what carries bytes and takes part in no protocol is
worth trying -- but a capture that hangs costs a real turn AND blocks the fleet's main loop,
so every failure here has to end up at an ordinary page rather than at a stuck one.

These tests drive the interception with a fake CDP session. They say nothing about how much
memory is saved: only a measurement on a real browser can say that, and this module's own
docstring refuses to claim the page "will not render".
"""
import os

import pytest

from relay import lean_capture as L


class FakeCdp:
    def __init__(self, fail_on=()):
        self.sent = []
        self.handlers = {}
        self.fail_on = set(fail_on)
        self.detached = False

    def on(self, event, fn):
        self.handlers[event] = fn

    def send(self, method, params=None):
        if method in self.fail_on:
            raise RuntimeError("cdp refused %s" % method)
        self.sent.append((method, params or {}))

    def detach(self):
        self.detached = True

    def methods(self):
        return [m for m, _ in self.sent]


def _interception(**kw):
    cdp = FakeCdp(**kw)
    return L._Interception(cdp, page=object()), cdp


def test_the_blocked_types_are_the_ones_that_carry_bytes_and_no_protocol():
    assert set(L.BLOCKED_TYPES) == {"Image", "Font", "Media", "Stylesheet"}


def test_script_and_the_transports_are_never_blocked():
    """The capture depends on the client running its own code, opening its own socket and
    sending its own chat frame. A template built from a crippled client describes a different
    product."""
    i, cdp = _interception()
    for kind in ("Script", "XHR", "Fetch", "WebSocket", "Document"):
        i._on_paused({"requestId": "r-%s" % kind, "resourceType": kind})
    assert cdp.methods() == ["Fetch.continueRequest"] * 5
    assert i.blocked == 0 and i.allowed == 5


def test_an_image_is_failed_not_continued():
    i, cdp = _interception()
    i._on_paused({"requestId": "r1", "resourceType": "Image"})
    assert cdp.sent == [("Fetch.failRequest",
                         {"requestId": "r1", "errorReason": "BlockedByClient"})]


def test_a_paused_request_is_always_answered_even_when_the_send_fails():
    """AN UNANSWERED PAUSE HANGS THE PAGE. The request waits for ever, the capture burns its
    whole timeout, and because a capture runs on the fleet's main loop nothing else is polled
    meanwhile. The handler must not raise, whatever the session does."""
    i, cdp = _interception(fail_on={"Fetch.failRequest"})
    i._on_paused({"requestId": "r1", "resourceType": "Image"})
    assert "Fetch.continueRequest" in cdp.methods(), "the request was left paused"
    assert i.errors == 1


def test_a_handler_that_keeps_failing_stops_intercepting_altogether():
    """Better an ordinary page than a broken interceptor holding requests. Fetch.disable
    releases everything still paused."""
    i, cdp = _interception(fail_on={"Fetch.failRequest", "Fetch.continueRequest"})
    for n in range(L.ERROR_BUDGET):
        i._on_paused({"requestId": "r%d" % n, "resourceType": "Image"})
    assert i.torn_down
    assert "Fetch.disable" in cdp.methods()


def test_an_event_without_a_request_id_is_ignored_rather_than_crashing():
    i, cdp = _interception()
    i._on_paused({"resourceType": "Image"})
    assert cdp.sent == []


def test_teardown_is_idempotent_and_detaches():
    i, cdp = _interception()
    i.teardown()
    i.teardown()
    assert cdp.methods().count("Fetch.disable") == 1
    assert cdp.detached


def test_teardown_survives_a_session_that_has_already_gone_away():
    """A browser crash or a closed context makes every call raise. Teardown still has to
    complete, because the caller runs it in a finally."""
    i, cdp = _interception(fail_on={"Fetch.disable"})
    i.teardown()
    assert i.torn_down


def test_install_returning_none_is_a_normal_outcome_not_an_error():
    """No interception means an ordinary page, which is what every capture did before this
    existed. Raising here would turn a cost optimisation into a way to fail a capture."""
    class Dead:
        @property
        def context(self):
            raise RuntimeError("context is gone")
    assert L.install(Dead()) is None


def test_pages_are_only_lean_inside_the_block(monkeypatch):
    seen = []
    monkeypatch.setattr(L, "install", lambda page: seen.append(page) or "i")
    L.maybe_install("before")
    with L.lean_pages():
        L.maybe_install("inside")
    L.maybe_install("after")
    assert seen == ["inside"], "a worker's own tab must never be made lean"


def test_the_block_collects_what_it_installed_so_the_caller_can_tear_it_down(monkeypatch):
    monkeypatch.setattr(L, "install", lambda page: "interception-for-%s" % page)
    with L.lean_pages() as lean:
        L.maybe_install("p1")
        L.maybe_install("p2")
    assert lean.installed == ["interception-for-p1", "interception-for-p2"]


def test_it_is_off_unless_the_flag_says_otherwise(monkeypatch):
    monkeypatch.delenv("MCP_CAPTURE_LEAN", raising=False)
    assert not L.enabled()
    assert L.capture_fn().__name__ == "capture_via_tab"
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MCP_CAPTURE_LEAN", off)
        assert not L.enabled(), off
    monkeypatch.setenv("MCP_CAPTURE_LEAN", "1")
    assert L.enabled()
    assert L.capture_fn().__name__ == "capture_via_lean_tab"


def test_the_choice_is_made_at_call_time_not_at_import(monkeypatch):
    """An operator setting the variable and restarting a run must get the new answer."""
    monkeypatch.setenv("MCP_CAPTURE_LEAN", "1")
    first = L.capture_fn()
    monkeypatch.setenv("MCP_CAPTURE_LEAN", "0")
    assert L.capture_fn() is not first


def test_the_route_asks_which_capture_rather_than_naming_one():
    """socket_route.py is frozen, so the policy about cost has to be applied by the caller."""
    import ast
    import inspect

    from relay import relay_fleet
    src = inspect.getsource(relay_fleet._socket_route)
    tree = ast.parse(src.strip())
    code = ast.dump(tree)
    assert "_choose_capture" in code
    assert "capture_via_tab" not in code, "the ordinary capture is named directly again"
