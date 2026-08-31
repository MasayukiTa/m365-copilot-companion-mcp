"""Hermetic unit tests for the edge auth-state classifier (F4).

No playwright, no live Edge -- synthetic page-state dicts only. Mirrors the
relay/selfimprove/test_guards.py style. Run from repo root:
    .venv\\Scripts\\python.exe -m relay.test_edge_auth
"""

import pytest
from relay import edge_auth as E


def test_f4_regression_redirect_with_chat_is_ready():
    # THE F4 regression: a CsrToSSR/auth=2 redirect URL but the chat UI has rendered AND a
    # generic sign-in affordance coexists. Must be "ready" (authed), NOT "needs_signin".
    # This exact assertion is what would have prevented today's wrong kill.
    state = {
        "url": "https://m365.cloud.microsoft/chat?redirfrom=CsrToSSR&auth=2",
        "ready_state": "complete",
        "has_chat_input": True,
        "has_signin": True,
    }
    assert E.classify_page(state) == "ready"
    assert E.recommended_action(E.classify_page(state)) == "proceed"
    print("ok test_f4_regression_redirect_with_chat_is_ready")


def test_chat_input_dominates_even_while_loading():
    # has_chat_input dominates every other signal, including a non-complete readyState.
    assert E.classify_page({"ready_state": "interactive", "has_chat_input": True}) == "ready"
    print("ok test_chat_input_dominates_even_while_loading")


def test_real_login_no_chat_is_needs_signin():
    state = {
        "url": "https://login.microsoftonline.com/common/oauth2/authorize?client_id=x",
        "ready_state": "complete",
        "has_chat_input": False,
        "has_signin": True,
    }
    assert E.classify_page(state) == "needs_signin"
    assert E.recommended_action("needs_signin") == "surface_signin"
    print("ok test_real_login_no_chat_is_needs_signin")


def test_signin_url_variants_needs_signin():
    for url in (
        "https://example.com/signin",
        "https://login.microsoftonline.com/x",
        "https://x/common/oauth2/authorize",
        "https://x/COMMON/OAUTH2/v2.0/authorize",  # case-insensitive
    ):
        state = {"url": url, "ready_state": "complete", "has_signin": True}
        assert E.classify_page(state) == "needs_signin", url
    print("ok test_signin_url_variants_needs_signin")


def test_csrtossr_redirect_no_chat_no_form_is_redirect():
    # CsrToSSR redirect, no chat yet, no sign-in form -> redirect (renavigate, do not kill).
    state = {
        "url": "https://m365.cloud.microsoft/?redirfrom=CsrToSSR&auth=2",
        "ready_state": "complete",
        "has_chat_input": False,
        "has_signin": False,
    }
    assert E.classify_page(state) == "redirect"
    assert E.recommended_action("redirect") == "renavigate"
    print("ok test_csrtossr_redirect_no_chat_no_form_is_redirect")


def test_redirect_with_signin_affordance_but_transient_url_is_redirect():
    # has_signin True but the URL is a transient redirect (not an active sign-in page) ->
    # redirect, not needs_signin. (e.g. an interstitial that flashes a sign-in link.)
    state = {
        "url": "https://m365.cloud.microsoft/chat?auth=2",
        "ready_state": "complete",
        "has_chat_input": False,
        "has_signin": True,
    }
    assert E.classify_page(state) == "redirect"
    print("ok test_redirect_with_signin_affordance_but_transient_url_is_redirect")


def test_incomplete_doc_is_loading():
    assert E.classify_page({"url": "https://x/signin", "ready_state": "loading"}) == "loading"
    assert E.classify_page({"ready_state": ""}) == "loading"
    assert E.recommended_action("loading") == "renavigate"
    print("ok test_incomplete_doc_is_loading")


def test_complete_but_nothing_decisive_is_loading():
    # Complete doc, plain url, no chat, no signin, no redirect markers -> loading.
    state = {"url": "https://m365.cloud.microsoft/chat", "ready_state": "complete"}
    assert E.classify_page(state) == "loading"
    print("ok test_complete_but_nothing_decisive_is_loading")


def test_recommended_action_mapping():
    assert E.recommended_action("ready") == "proceed"
    assert E.recommended_action("redirect") == "renavigate"
    assert E.recommended_action("loading") == "renavigate"
    assert E.recommended_action("needs_signin") == "surface_signin"
    assert E.recommended_action("anything_unknown") == "renavigate"
    print("ok test_recommended_action_mapping")


def test_url_helpers_case_insensitive():
    assert E._looks_like_signin("HTTPS://LOGIN.MICROSOFTONLINE.COM/x")
    assert E._looks_like_signin("https://x/SignIn")
    assert not E._looks_like_signin("https://m365.cloud.microsoft/chat?auth=2")
    assert E._looks_like_redirect("https://x/?redirFrom=CsrToSSR&AUTH=2")
    assert E._looks_like_redirect("https://x/?auth=2")
    assert not E._looks_like_redirect("https://m365.cloud.microsoft/chat")
    print("ok test_url_helpers_case_insensitive")


def test_defensive_against_empty_and_missing():
    assert E.classify_page({}) == "loading"
    assert E.classify_page(None) == "loading"
    assert E._looks_like_signin(None) is False
    assert E._looks_like_redirect(None) is False
    print("ok test_defensive_against_empty_and_missing")


if __name__ == "__main__":
    test_f4_regression_redirect_with_chat_is_ready()
    test_chat_input_dominates_even_while_loading()
    test_real_login_no_chat_is_needs_signin()
    test_signin_url_variants_needs_signin()
    test_csrtossr_redirect_no_chat_no_form_is_redirect()
    test_redirect_with_signin_affordance_but_transient_url_is_redirect()
    test_incomplete_doc_is_loading()
    test_complete_but_nothing_decisive_is_loading()
    test_recommended_action_mapping()
    test_url_helpers_case_insensitive()
    test_defensive_against_empty_and_missing()
    print("ALL EDGE AUTH TESTS PASSED")


#: Captured before the guard below replaces it, so the two tests that exercise the navigation
#: helper itself can still reach the real function.
_REAL_NAVIGATE = E._navigate
_REAL_SURFACE_WITH_WAY_BACK = E._surface_with_a_way_back


@pytest.fixture(autouse=True)
def _never_touch_a_real_browser(monkeypatch):
    """No test here may navigate the operator's Edge.

    One did. It stubbed classify_live and edge_recover but not _navigate, so the retry branch
    drove the real CDP endpoint on :9222 and left the companion Edge sitting on
    https://example/agent -- the agent page gone, the fleet's browser broken, discovered only
    because the next live check said "loading". Same shape as a fixture here that once posted
    `rm -rf /` into the real approval queue: the stub covered the part being tested and left a
    live side door open.
    """
    monkeypatch.setattr(E, "_navigate", lambda cdp_url, agent_url: True)
    # And no test may leave a window up: surfacing is replaced wholesale unless a test
    # deliberately exercises it.
    monkeypatch.setattr(E, "_surface_with_a_way_back",
                        lambda cdp_url, agent_url, rehide_after_s=0: True)


# THE STUBS BELOW MUST USE THE KEY classify_live ACTUALLY RETURNS ("cls", not "class").
# They were first written with "class", the code read "class", and all four tests passed --
# while the live browser, which was ready, came back "unknown". A stub that agrees with the
# code instead of with the thing it stands in for tests nothing.


def test_ensure_ready_returns_ready_without_navigating_when_a_tab_is_already_ready(monkeypatch):
    """The classifier said "renavigate" and nothing in the system ever did it.

    classify_live and recommended_action were called only by the CLI, by a health reader and
    by tests, so the product computed the right action, showed a red dot, and waited for a
    person. Measured 2026-08-31: the companion Edge sat on about:blank, the doctor called it
    "sign-in needed", and a benchmark run started against it -- every worker got the default
    assistant with no tools and wrote patches from memory. The page needed one navigation.
    """
    monkeypatch.setattr(E, "classify_live", lambda cdp_url="x": [{"cls": "ready"}])
    called = []
    assert E.ensure_ready("https://example/agent") == "ready"
    assert not called


def test_ensure_ready_hands_back_needs_signin_without_looping(monkeypatch):
    """The one state a person must resolve. Navigating past it would loop forever."""
    monkeypatch.setattr(E, "classify_live", lambda cdp_url="x": [{"cls": "needs_signin"}])
    assert E.ensure_ready("https://example/agent") == "needs_signin"



def test_ensure_ready_survives_a_dead_browser(monkeypatch):
    """A recovery that raises when the thing it recovers is absent is not a recovery."""
    def _boom(cdp_url="x"):
        raise OSError("no CDP")
    monkeypatch.setattr(E, "classify_live", _boom)
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert E.ensure_ready("https://example/agent", attempts=2, settle_s=0) == "unknown"


def test_the_window_is_surfaced_only_after_the_automatic_path_has_failed(monkeypatch):
    """The fallback is a last resort, and it was never reachable for the fleet's browser.

    bridge/copilot_bridge.py has this chain -- bounded retries, a success-only latch, a paired
    rehide -- pointed at the bridge's Edge on :9223; its own comment notes that the fleet is on
    :9222. So the fleet's browser had the primitives and nothing driving them: when it needed a
    human, there was no retry, no window, and no message. Only a coloured dot.

    Surfacing on the FIRST transient needs_signin would be its own failure: a window that jumps
    forward for something that would have cleared itself is one people learn to dismiss.
    """
    calls = []
    monkeypatch.setattr(E, "classify_live", lambda cdp_url="x": [{"cls": "needs_signin"}])
    monkeypatch.setattr(E, "_surface_with_a_way_back",
                        lambda cdp_url, agent_url, rehide_after_s=0: calls.append(cdp_url))

    assert E.ensure_ready("https://example/agent", attempts=3, settle_s=0) == "needs_signin"
    assert len(calls) == 1, (
        "expected exactly one surface, on the last attempt: %r" % calls)
    assert "9222" in calls[0], "surfaced the wrong browser: %r" % calls


def test_the_fallback_can_be_switched_off(monkeypatch):
    """A health probe that merely reports should not yank a window forward."""
    monkeypatch.setattr(E, "classify_live", lambda cdp_url="x": [{"cls": "needs_signin"}])
    calls = []
    monkeypatch.setattr(E, "_surface_with_a_way_back",
                        lambda cdp_url, agent_url, rehide_after_s=0: calls.append(cdp_url))
    E.ensure_ready("https://example/agent", attempts=2, settle_s=0, surface_on_signin=False)
    assert calls == []


def test_the_port_comes_from_the_cdp_url_not_a_default():
    """Surfacing the wrong browser is the failure the bridge's comment warns about."""
    assert E._port_of("http://127.0.0.1:9222") == 9222
    assert E._port_of("http://127.0.0.1:9223") == 9223
    assert E._port_of("nonsense") == 9222


def test_navigate_tries_both_verbs_because_builds_differ(monkeypatch):
    """/json/new is a GET on some Edge builds and a PUT on others.

    Measured on this machine: GET answered 405 Method Not Allowed and PUT opened the page --
    and the opposite had worked an hour earlier. A helper that knows only one verb reports
    "could not navigate" on a browser that would have navigated.

    _navigate is exercised directly here. Reaching it through ensure_ready would run against
    the autouse guard's stub, which is what the previous version of this test did.
    """
    seen = []

    def _urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        method = "GET" if isinstance(req, str) else req.get_method()
        seen.append(method)
        if method == "GET":
            raise OSError("405 Method Not Allowed")

        class _R:
            def read(self_inner):
                return b""
        return _R()

    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", _urlopen)
    assert _REAL_NAVIGATE("http://127.0.0.1:9222", "https://example/agent") is True
    assert seen == ["GET", "PUT"], seen


def test_navigate_reports_failure_when_neither_verb_works(monkeypatch):
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no browser")))
    assert _REAL_NAVIGATE("http://127.0.0.1:9222", "https://example/agent") is False


def test_every_surface_schedules_a_return_to_background(monkeypatch):
    """A surfaced window that nobody puts back stays on the taskbar until the process dies.

    bridge/copilot_bridge.py records that exact bug against its own earlier code, and the
    first version of the fleet's fallback reproduced it: the owner found the fleet's Edge
    sitting in the taskbar. rehide() alone is not enough either -- measured, it minimises the
    window while the process stays headed, so the taskbar entry remains. Returning to true
    background is a relaunch in headless mode.
    """
    started = {}

    # PATCH THE REAL MODULE'S ATTRIBUTES. `from relay.edge_recover import surface` reads the
    # attribute already bound on the `relay` package, so swapping the sys.modules entry is
    # invisible -- and the real surface() then brought the operator's browser forward. Same
    # mistake as the router tests, one module along.
    import relay.edge_recover as _rec
    monkeypatch.setattr(_rec, "surface", lambda port=None, open_url="": True)
    monkeypatch.setattr(_rec, "rehide", lambda port=None: started.setdefault("rehide", port))

    class _Timer:
        def __init__(self, delay, fn):
            started["delay"] = delay
            started["fn"] = fn
            self.daemon = False

        def start(self):
            started["started"] = True

    import threading
    monkeypatch.setattr(threading, "Timer", _Timer)
    assert _REAL_SURFACE_WITH_WAY_BACK("http://127.0.0.1:9222", "https://x", 5.0) is True
    assert started.get("started") is True, "nothing was scheduled to put the window back"
    assert started["delay"] == 5.0


def test_the_navigation_url_actually_has_its_query_separator():
    """THE BUG EVERY OTHER TEST HERE MISSED.

    _navigate built the url with urljoin(cdp, "/json/new?"), and urljoin DROPS the trailing
    "?". The result was ".../json/new" + the agent url -- ".../json/newhttps://..." -- a 404 on
    every call, GET and PUT alike. So _navigate returned False every time it was ever called,
    ensure_ready never repaired anything, and each recovery had to be done by hand.

    The other tests stubbed urlopen and asserted that it was CALLED. None looked at what it was
    called with. A stub that agrees with the code tests the stub.
    """
    seen = []

    def _urlopen(req, timeout=None):
        seen.append(req if isinstance(req, str) else req.full_url)
        raise OSError("stop here; the url is what matters")

    import urllib.request as _req
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(_req, "urlopen", _urlopen)
        _REAL_NAVIGATE("http://127.0.0.1:9222", "https://example.test/chat/?titleId=T_x")
    assert seen, "urlopen was never called"
    for url in seen:
        assert "/json/new?" in url, "the query separator is missing: %r" % url
        assert url.endswith("https://example.test/chat/?titleId=T_x"), url
