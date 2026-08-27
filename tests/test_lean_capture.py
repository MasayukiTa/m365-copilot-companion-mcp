"""Blocking the pixels on the capture page must never cost a turn.

The capture page is the socket route's last browser cost: one page, once per token lifetime,
on which a real turn runs. A browser with such a page open measured 697 MB against 341 MB
without, across 4,549 samples. Blocking what carries bytes and takes part in no protocol is
worth trying -- but a capture that hangs costs a real turn AND blocks the fleet's main loop,
so every failure here has to end up at an ordinary page rather than at a stuck one.

These tests drive the handler with fake routes. They say nothing about how much memory is
saved: only a measurement on a real browser can say that, and the first one said none at all.
"""
import pytest

from relay import lean_capture as L


class FakeRequest:
    def __init__(self, kind):
        self.resource_type = kind


class FakeRoute:
    """A route as page.route hands one over: abort it or let it through."""

    def __init__(self, kind, fail_on=()):
        self.request = FakeRequest(kind)
        self.fail_on = set(fail_on)
        self.calls = []

    def abort(self, *a):
        self.calls.append("abort")
        if "abort" in self.fail_on:
            raise RuntimeError("route.abort refused")

    def continue_(self, *a, **k):
        self.calls.append("continue")
        if "continue" in self.fail_on:
            raise RuntimeError("route.continue_ refused")

    def fallback(self, *a, **k):
        self.calls.append("fallback")
        if "fallback" in self.fail_on:
            raise RuntimeError("route.fallback refused")


class FakePage:
    def __init__(self, fail_on=()):
        self.routed = []
        self.unrouted = []
        self.fail_on = set(fail_on)

    def route(self, pattern, handler):
        if "route" in self.fail_on:
            raise RuntimeError("cannot route")
        self.routed.append((pattern, handler))

    def unroute(self, pattern, handler=None):
        if "unroute" in self.fail_on:
            raise RuntimeError("cannot unroute")
        self.unrouted.append(pattern)


def _interception(page_fail=()):
    page = FakePage(page_fail)
    return L._Interception(page), page


def _send(interception, kind, fail_on=()):
    route = FakeRoute(kind, fail_on)
    interception.handle(route)
    return route


def test_the_blocked_types_are_the_ones_that_carry_bytes_and_no_protocol(monkeypatch):
    """LOWER CASE, because that is how Playwright spells a resource type. The first version
    used CDP's capitalised spelling, where a type matching nothing fails silently and looks
    exactly like a change that saved no memory."""
    assert set(L.BLOCKED_TYPES) <= {"image", "font", "media"}


def test_stylesheet_is_not_blocked_by_default():
    """MEASURED, not assumed. Blocking it stopped the composer from rendering at all -- five
    captures in five failed, each after the opener spent its full 75 seconds on a page that
    was never going to finish. scripts/win/lean_capture_isolate.py has the per-type table.
    A blocked stylesheet does not make a lighter page; it makes one that never loads."""
    assert "stylesheet" not in L.BLOCKED_TYPES


def test_the_set_can_be_narrowed_without_editing_the_code(monkeypatch):
    """It has already had to be narrowed once by measurement, and the next agent surface may
    narrow it again."""
    import importlib
    monkeypatch.setenv("MCP_CAPTURE_LEAN_TYPES", "image")
    importlib.reload(L)
    try:
        assert L.BLOCKED_TYPES == ("image",)
    finally:
        monkeypatch.delenv("MCP_CAPTURE_LEAN_TYPES", raising=False)
        importlib.reload(L)


def test_script_and_the_transports_are_never_blocked():
    """The capture depends on the client running its own code, opening its own socket and
    sending its own chat frame. A template built from a crippled client describes a different
    product."""
    i, _ = _interception()
    for kind in ("script", "xhr", "fetch", "websocket", "document"):
        assert _send(i, kind).calls == ["fallback"], kind
    assert i.blocked == 0 and i.allowed == 5


def test_an_image_is_aborted_not_continued():
    i, _ = _interception()
    assert _send(i, "image").calls == ["abort"]
    assert i.blocked == 1


def test_a_request_is_always_answered_even_when_the_abort_fails():
    """AN UNANSWERED REQUEST HANGS THE PAGE. It waits for ever, the capture burns its whole
    timeout, and because a capture runs on the fleet's main loop nothing else is polled
    meanwhile. The handler must not raise, whatever the route does."""
    i, _ = _interception()
    route = _send(i, "image", fail_on={"abort"})
    assert route.calls == ["abort", "fallback"], "the request was left unanswered"
    assert i.errors == 1


def test_a_handler_that_keeps_failing_stops_intercepting_altogether():
    """Better an ordinary page than a broken interceptor. Unrouting releases the page to load
    as it normally would."""
    i, page = _interception()
    for _ in range(L.ERROR_BUDGET):
        _send(i, "image", fail_on={"abort", "fallback", "continue"})
    assert i.torn_down
    assert page.unrouted == ["**/*"]


def test_a_request_whose_type_cannot_be_read_is_let_through():
    """Unknown is not a reason to block: a type nobody could read might be the chat socket."""
    class Opaque:
        def __init__(self):
            self.calls = []

        @property
        def request(self):
            raise RuntimeError("gone")

        def abort(self):
            self.calls.append("abort")

        def continue_(self):
            self.calls.append("continue")

        def fallback(self):
            self.calls.append("fallback")

    i, _ = _interception()
    route = Opaque()
    i.handle(route)
    assert route.calls == ["fallback"]


def test_teardown_is_idempotent():
    i, page = _interception()
    i.teardown()
    i.teardown()
    assert page.unrouted == ["**/*"]


def test_teardown_survives_a_page_that_has_already_gone_away():
    """A browser crash or a closed context makes every call raise. Teardown still has to
    complete, because the caller runs it in a finally."""
    i, _ = _interception(page_fail={"unroute"})
    i.teardown()
    assert i.torn_down


def test_a_torn_down_interception_stops_blocking_even_if_a_route_still_arrives():
    """unroute is not instantaneous; a request already in flight can reach a handler that has
    given up. It must be let through, not aborted by a component that has stood down."""
    i, _ = _interception()
    i.teardown()
    assert _send(i, "image").calls == ["fallback"]


def test_install_returning_none_is_a_normal_outcome_not_an_error():
    """No interception means an ordinary page, which is what every capture did before this
    existed. Raising here would turn a cost optimisation into a way to fail a capture."""
    assert L.install(FakePage(fail_on={"route"})) is None


def test_install_routes_everything_and_hands_back_the_interception():
    page = FakePage()
    i = L.install(page)
    assert i is not None
    assert [p for p, _ in page.routed] == ["**/*"]


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
    code = ast.dump(ast.parse(src.strip()))
    assert "_choose_capture" in code
    assert "capture_via_tab" not in code, "the ordinary capture is named directly again"


def test_the_interception_is_not_driven_through_the_sync_playwright_api_from_a_callback():
    """WHAT THE FIRST LIVE TRIAL FOUND. Driving CDP directly meant calling cdp.send() from
    inside a Fetch.requestPaused handler -- a nested call into the event loop already running
    it -- and the sync api answered with CancelledError. One capture in two failed and the
    other took 90 seconds against 34. page.route is the sync api's own answer."""
    from _srcprobe import executable_source

    code = executable_source(L)
    assert "Fetch.enable" not in code and "Fetch.requestPaused" not in code
    assert "new_cdp_session" not in code
