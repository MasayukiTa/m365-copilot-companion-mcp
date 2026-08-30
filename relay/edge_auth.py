"""Edge/Copilot auth-state classifier (F4 countermeasure).

Fixes failure F4 (bench/M365_HARDENING_AND_UX.md): M365 Copilot tabs on URLs like
``?redirfrom=CsrToSSR&auth=2`` were misread as "SSO expired" and a measurement run was
wrongly killed -- when the tabs were in fact AUTHENTICATED (the chat UI had loaded).

The lesson encoded here: **classify auth state from PAGE STATE, not from the URL.** A
redirect param in the URL says nothing about whether the user is signed in. The single
decisive signal is whether the Copilot chat composer/input has rendered: if it has, we are
authed and usable, full stop -- any redirect params are irrelevant residue.

Layering:
  * pure core  -- ``classify_page`` / ``recommended_action`` + url helpers. stdlib only,
    deterministic, no playwright, no network. This is what the tests exercise.
  * thin adapter -- ``probe_page_state`` / ``classify_live``. Lazily imports playwright
    INSIDE the functions so the module imports without playwright installed and so the
    tests never touch the live Edge. Wrapped in try/except so a flaky page never throws.
  * CLI         -- ``python -m relay.edge_auth`` prints ``classify_live()`` for the
    companion Edge as a human diagnostic, guarded so failures print cleanly.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Pure core (stdlib only -- no playwright, no network)
# --------------------------------------------------------------------------- #

# URL fragments that mean "an actual interactive sign-in / account-pick surface is
# showing" -- the only situation where a human is genuinely required.
_SIGNIN_MARKERS = (
    "/signin",
    "login.microsoftonline",
    "oauth2/authorize",
    "/common/oauth2",
)

# URL fragments that mean "an auth/SSO redirect is mid-flight" -- transient, the caller
# should re-navigate and wait for the chat to render, NOT declare expiry.
_REDIRECT_MARKERS = (
    "csrtossr",
    "redirfrom",
    "auth=",
)


def _looks_like_signin(url: str) -> bool:
    """True if *url* looks like an active sign-in / account-pick page (case-insensitive)."""
    u = (url or "").lower()
    return any(m in u for m in _SIGNIN_MARKERS)


def _looks_like_redirect(url: str) -> bool:
    """True if *url* looks like a known transient auth/SSO redirect (case-insensitive)."""
    u = (url or "").lower()
    return any(m in u for m in _REDIRECT_MARKERS)


def classify_page(state: dict) -> str:
    """Classify Copilot tab auth state from a page-state dict.

    Returns one of: ``"ready"``, ``"needs_signin"``, ``"loading"``, ``"redirect"``.

    ``state`` keys (all optional -- be defensive):
      * ``url``            (str)
      * ``ready_state``    (str: "complete"/...)
      * ``has_chat_input`` (bool) -- a chat composer/input is present
      * ``has_signin``     (bool) -- a sign-in affordance is present
      * ``title``          (str)
    """
    state = state or {}
    url = state.get("url") or ""
    ready_state = state.get("ready_state") or ""
    has_chat_input = bool(state.get("has_chat_input"))
    has_signin = bool(state.get("has_signin"))

    # RULE 1 (dominant): chat rendered == authed. The chat-input signal dominates every
    # other signal -- URL redirect params are irrelevant, and a generic "has_signin" (an
    # account menu / footer "sign in" link that can coexist with a loaded chat) must NOT
    # be treated as needs_signin while a chat input is present. This rule alone is the F4
    # fix: ?redirfrom=CsrToSSR&auth=2 + has_chat_input -> "ready".
    if has_chat_input:
        return "ready"

    # RULE 2: nothing decisive until the document has finished loading.
    if ready_state != "complete":
        return "loading"

    # RULE 3: a sign-in affordance with no chat. Only a real, active sign-in/account URL
    # means a human is required; otherwise it's a transient redirect (e.g. CsrToSSR,
    # auth=2) and the caller should just re-navigate.
    if has_signin and not has_chat_input:
        if _looks_like_signin(url):
            return "needs_signin"
        return "redirect"

    # RULE 4: no chat, no sign-in form, but the URL is a known redirect -> mid-flight.
    if _looks_like_redirect(url) or _looks_like_signin(url):
        return "redirect"

    # RULE 5: complete doc, nothing decisive yet.
    return "loading"


def recommended_action(cls: str) -> str:
    """Map a classification to a caller action.

    ``ready`` -> ``"proceed"``; ``redirect``/``loading`` -> ``"renavigate"``;
    ``needs_signin`` -> ``"surface_signin"`` (the ONLY state that needs the human).
    """
    if cls == "ready":
        return "proceed"
    if cls == "needs_signin":
        return "surface_signin"
    # redirect, loading, and any unknown value -> re-navigate / keep waiting, never kill.
    return "renavigate"


# --------------------------------------------------------------------------- #
# Thin playwright adapter (NOT imported at module top; NOT exercised by tests)
# --------------------------------------------------------------------------- #

_CHAT_INPUT_SELECTOR = "textarea, [contenteditable='true'], [role='textbox']"
_SIGNIN_SELECTOR = (
    "input[type='password'], "
    "#i0116, "  # AAD account/email field
    "[data-testid='i0116'], "
    "text=/sign in/i, "
    "text=/サインイン/"
)


def probe_page_state(page) -> dict:
    """Build a page-state dict from a live playwright ``Page``.

    Every access is wrapped in try/except so a flaky page never throws; missing signals
    degrade gracefully (url=""/ready_state=""/booleans False). playwright is imported
    lazily by the caller (``classify_live``); this function itself touches only the passed
    ``page`` object, so the module imports fine without playwright installed.
    """
    state = {
        "url": "",
        "ready_state": "",
        "has_chat_input": False,
        "has_signin": False,
        "title": "",
    }
    try:
        state["url"] = page.url or ""
    except Exception:
        pass
    try:
        rs = page.evaluate("document.readyState")
        state["ready_state"] = rs or ""
    except Exception:
        pass
    try:
        state["has_chat_input"] = page.locator(_CHAT_INPUT_SELECTOR).count() > 0
    except Exception:
        pass
    try:
        state["has_signin"] = page.locator(_SIGNIN_SELECTOR).count() > 0
    except Exception:
        pass
    try:
        state["title"] = page.title() or ""
    except Exception:
        pass
    return state


def classify_live(cdp_url: str = "http://127.0.0.1:9222") -> list[dict]:
    """Connect to a running Edge over CDP, probe every page, classify each.

    Returns ``[{"url", "cls", "action"}, ...]``. This is the LIVE path -- it is never
    called by the tests. playwright is imported lazily here so the module imports without
    playwright installed.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps module import clean

    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            for context in browser.contexts:
                for page in context.pages:
                    state = probe_page_state(page)
                    cls = classify_page(state)
                    results.append(
                        {
                            "url": state.get("url", ""),
                            "cls": cls,
                            "action": recommended_action(cls),
                        }
                    )
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return results


# --------------------------------------------------------------------------- #
# CLI -- human diagnostic. Guarded so import/connection failures print cleanly.
# --------------------------------------------------------------------------- #

def _main(cdp_url: str = "http://127.0.0.1:9222") -> int:
    try:
        rows = classify_live(cdp_url)
    except ImportError:
        print("edge_auth: playwright is not installed; cannot probe the live Edge.")
        return 1
    except Exception as exc:  # connection refused, no Edge, CDP error, ...
        print("edge_auth: could not connect to Edge at %s (%s)." % (cdp_url, exc))
        return 1
    if not rows:
        print("edge_auth: connected, but found no open pages.")
        return 0
    for r in rows:
        print("[%-12s] %-14s %s" % (r["cls"], r["action"], r["url"]))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())


# --------------------------------------------------------------------------- #
# Acting on the classification
# --------------------------------------------------------------------------- #

def _port_of(cdp_url: str, default: int = 9222) -> int:
    """The port in a CDP url. The fleet is on 9222 and the bridge on 9223; surfacing the wrong
    one yanks a browser the operator was not looking for."""
    try:
        import urllib.parse as _p
        return int(_p.urlsplit(cdp_url).port or default)
    except Exception:
        return default


def _navigate(cdp_url: str, agent_url: str) -> bool:
    """Open the agent in the browser at `cdp_url`. /json/new is a PUT on newer builds and a
    GET on older ones, so try both before reporting failure."""
    import urllib.parse as _parse
    import urllib.request as _req
    url = _parse.urljoin(cdp_url, "/json/new?") + agent_url
    try:
        _req.urlopen(url, timeout=20).read()
        return True
    except Exception:
        pass
    try:
        _req.urlopen(_req.Request(url, method="PUT"), timeout=20).read()
        return True
    except Exception:
        return False


def ensure_ready(agent_url: str, cdp_url: str = "http://127.0.0.1:9222",
                 attempts: int = 3, settle_s: float = 8.0,
                 surface_on_signin: bool = True) -> str:
    """Make the companion Edge ready, and return the final classification.

    NOTHING ACTED ON `recommended_action` UNTIL THIS EXISTED. classify_live and
    recommended_action were called only by the CLI, by a health reader, and by tests -- so the
    system computed "renavigate", displayed a red dot, and waited for a person.

    Measured 2026-08-31: the companion Edge sat on about:blank; the doctor reported "M365
    sign-in needed" and I asked the owner to sign in by hand. The page did not need a sign-in.
    It needed one navigation, which took eight seconds. Meanwhile a benchmark run started
    against a blank page: every worker got the default assistant with no tools, reported
    STUCK, and wrote patches from memory.

    Only `needs_signin` requires a human. Everything else is this function's job.
    """
    import json as _json
    import time as _time
    import urllib.parse as _parse
    import urllib.request as _req

    last = "unknown"
    for _attempt in range(max(1, attempts)):
        try:
            rows = classify_live(cdp_url)
        except Exception:
            rows = []
        # A single ready tab is enough: the others are blanks the browser keeps around.
        if any(r.get("cls") == "ready" for r in rows):
            return "ready"
        last = next((r.get("cls") for r in rows), "unknown") or "unknown"
        if last == "needs_signin":
            # NOT ON THE FIRST SIGHTING. needs_signin also shows up mid-redirect, and a window
            # that jumps forward for something that would have cleared itself is one people
            # learn to dismiss. Retry like any other state; only the LAST attempt is treated
            # as a genuine failure.
            if _attempt < max(1, attempts) - 1:
                _navigate(cdp_url, agent_url)
                _time.sleep(settle_s)
                continue
            # THE LAST RESORT, AFTER THE AUTOMATIC PATH HAS GENUINELY FAILED.
            #
            # bridge/copilot_bridge.py has this chain -- bounded retries, a latch that only
            # sets on a truthful success, a paired rehide so the window cannot stay foreground
            # -- and it is pointed at the BRIDGE's browser on :9223. Its own comment says so:
            # "fleet's :9222 -- omitting port would surface the wrong browser." The fleet's
            # browser had the primitives and nothing driving them, so when it needed a human
            # nobody was told: no retry, no window, just a coloured dot on a panel.
            if surface_on_signin:
                try:
                    from relay.edge_recover import surface
                    surface(port=_port_of(cdp_url), open_url=agent_url)
                except Exception:
                    pass
            return "needs_signin"          # the one state a person must resolve

        if not _navigate(cdp_url, agent_url):
            return last
        _time.sleep(settle_s)

    try:
        rows = classify_live(cdp_url)
        if any(r.get("cls") == "ready" for r in rows):
            return "ready"
        return next((r.get("cls") for r in rows), last) or last
    except Exception:
        return last
