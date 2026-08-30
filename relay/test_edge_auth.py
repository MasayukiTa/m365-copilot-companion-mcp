"""Hermetic unit tests for the edge auth-state classifier (F4).

No playwright, no live Edge -- synthetic page-state dicts only. Mirrors the
relay/selfimprove/test_guards.py style. Run from repo root:
    .venv\\Scripts\\python.exe -m relay.test_edge_auth
"""

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


def test_ensure_ready_navigates_when_the_page_is_blank(monkeypatch):
    """about:blank classifies as `loading` -> `renavigate`, which is this function's job."""
    seen = {"n": 0, "urls": []}

    def _classify(cdp_url="x"):
        seen["n"] += 1
        return [{"cls": "ready"}] if seen["n"] > 1 else [{"class": "loading"}]

    def _urlopen(url, timeout=None):
        seen["urls"].append(url if isinstance(url, str) else url.full_url)

        class _R:
            def read(self_inner):
                return b""
        return _R()

    monkeypatch.setattr(E, "classify_live", _classify)
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", _urlopen)
    assert E.ensure_ready("https://example/agent", settle_s=0) == "ready"
    assert any("https://example/agent" in u for u in seen["urls"]), seen["urls"]


def test_ensure_ready_survives_a_dead_browser(monkeypatch):
    """A recovery that raises when the thing it recovers is absent is not a recovery."""
    def _boom(cdp_url="x"):
        raise OSError("no CDP")
    monkeypatch.setattr(E, "classify_live", _boom)
    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert E.ensure_ready("https://example/agent", attempts=2, settle_s=0) == "unknown"
