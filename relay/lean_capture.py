"""Open the capture tab without the pixels: block images, fonts, media and stylesheets.

WHAT THIS IS FOR. The socket route is built from a tab. Once per token lifetime -- 15 to 79
minutes, measured -- a page is opened on the agent surface, one real turn is run on it, the
token and the request template are read out of the websocket, and the page is closed. That
page is the last browser cost the socket route has, and while it is open it is a full M365
Copilot document: the difference between a browser with such a page and one without measured
341 MB against 697 MB across 4,549 samples.

WHAT IS BLOCKED AND WHAT IS NOT. Image, Font, Media, Stylesheet. Not Script, not XHR, not
Fetch, not WebSocket, and not the document: the capture depends on the client running its own
code, opening its own socket and sending its own chat frame, and a request template built from
a crippled client is a template that describes a different product. The blocked types are the
ones that carry bytes into memory and take part in no protocol.

WHAT THIS DOES NOT CLAIM. It does not stop the page rendering. The DOM is still built, the
scripts still run, layout and paint still happen, and anything the client draws itself is
still drawn. "It will not render" would be a nice story and is not true; the only things that
decide whether this is worth keeping are the measured RSS and the measured lifetime.

FAILING OPEN, DELIBERATELY. Every failure here falls back to an ordinary full page. A blocked
request that cannot be answered leaves the page hanging until the capture times out, which
costs a real turn -- so the handler cannot raise, an error budget disables interception
entirely rather than letting requests pile up behind a broken handler, and installation
failure means no interception at all rather than a half-installed one.

EXPERIMENTAL, AND OFF UNTIL MEASURED. MCP_CAPTURE_LEAN=1 turns it on.
"""
from __future__ import annotations

import os
import threading

#: The resource types that carry bytes and take part in no protocol. CDP spells them exactly
#: like this; a misspelt type silently matches nothing, which is why they are listed once.
BLOCKED_TYPES = ("Image", "Font", "Media", "Stylesheet")

#: How many handler errors are tolerated before interception is torn down. A handler that has
#: started failing must not be allowed to hold requests: the page would hang until the capture
#: timeout, and a hung capture blocks the fleet's main loop.
ERROR_BUDGET = 5


def enabled() -> bool:
    return os.environ.get("MCP_CAPTURE_LEAN", "0").strip().lower() not in ("0", "false", "no", "off", "")


_LEAN = threading.local()


class _Interception:
    """One page's interception, with the teardown that must happen on every exit path."""

    def __init__(self, cdp, page):
        self.cdp, self.page = cdp, page
        self.blocked = 0
        self.allowed = 0
        self.errors = 0
        self.torn_down = False

    def _on_paused(self, event):
        # NEVER RAISES. An exception here leaves the request paused for ever and the page
        # waiting on it; the capture then burns its whole timeout and a real turn with it.
        rid = event.get("requestId")
        if not rid:
            return
        try:
            if (event.get("resourceType") or "") in BLOCKED_TYPES:
                self.cdp.send("Fetch.failRequest",
                              {"requestId": rid, "errorReason": "BlockedByClient"})
                self.blocked += 1
            else:
                self.cdp.send("Fetch.continueRequest", {"requestId": rid})
                self.allowed += 1
        except Exception:
            self.errors += 1
            try:
                self.cdp.send("Fetch.continueRequest", {"requestId": rid})
            except Exception:
                pass
            if self.errors >= ERROR_BUDGET:
                # Stop interfering rather than keep failing. Fetch.disable releases everything
                # still paused, so the page finishes loading as an ordinary one.
                self.teardown()

    def teardown(self):
        """Idempotent, and called on every exit -- success, timeout, cancellation, crash."""
        if self.torn_down:
            return
        self.torn_down = True
        try:
            self.cdp.send("Fetch.disable")
        except Exception:
            pass
        try:
            self.cdp.detach()
        except Exception:
            pass

    def stats(self) -> dict:
        return {"blocked": self.blocked, "allowed": self.allowed, "errors": self.errors,
                "torn_down": self.torn_down}


def install(page):
    """Start blocking on `page`. Returns the interception, or None if it could not start.

    None is not an error condition for the caller: no interception means an ordinary page,
    which is exactly what every capture did before this module existed.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        interception = _Interception(cdp, page)
        cdp.on("Fetch.requestPaused", interception._on_paused)
        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        return interception
    except Exception:
        return None


def maybe_install(page):
    """Install only if this thread asked for a lean page. Called from the ordinary opener.

    A thread-local rather than a parameter because the opener is reached through a FROZEN
    function -- relay/socket_route.py's capture_via_tab -- whose signature cannot change. The
    flag is set for the duration of one open, so a worker's own tab is never affected even
    when the two run side by side.
    """
    if not getattr(_LEAN, "on", False):
        return None
    got = install(page)
    holder = getattr(_LEAN, "holder", None)
    if holder is not None and got is not None:
        holder.append(got)
    return got


class lean_pages:
    """Mark pages opened by THIS thread, inside this block, as lean."""

    def __init__(self):
        self.installed = []

    def __enter__(self):
        _LEAN.on = True
        _LEAN.holder = self.installed
        return self

    def __exit__(self, *exc):
        _LEAN.on = False
        _LEAN.holder = None
        return False


def capture_via_lean_tab(context, agent_url):
    """A drop-in for socket_route.capture_via_tab that blocks the pixels while it works.

    SAME CONTRACT, INCLUDING THE FAILURES. It opens a tab, captures, and closes the tab --
    the page is never kept. Every exception the ordinary path can raise reaches the caller
    unchanged, so the route's circuit breaker sees the same events it always saw.

    THE TEARDOWN IS THE POINT OF THE STRUCTURE. A capture can end by returning, by raising, by
    timing out, or by the browser going away underneath it. The interception is dropped and
    the page closed on all four, in that order: dropping interception first means that if the
    close hangs, nothing is left holding requests.
    """
    from relay.chathub_capture import capture
    from relay.relay_fleet import _open_fresh

    page, interceptions = None, []
    try:
        with lean_pages() as lean:
            page = _open_fresh(context, agent_url)
            interceptions = lean.installed
        return capture(page)
    finally:
        for i in interceptions:
            try:
                i.teardown()
            except Exception:
                pass
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def capture_fn():
    """The capture the route should use, chosen once. Ordinary unless the flag says otherwise.

    Named as a function rather than resolved at import so a test -- and an operator setting the
    variable in a running shell -- gets the answer that is true now.
    """
    if enabled():
        return capture_via_lean_tab
    from relay.socket_route import capture_via_tab
    return capture_via_tab
