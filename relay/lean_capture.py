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

HOW IT INTERCEPTS, AND WHY NOT THE OBVIOUS WAY. The first version drove CDP directly --
Fetch.enable, then Fetch.failRequest or Fetch.continueRequest from the Fetch.requestPaused
handler. It half worked and was much worse: one capture in two failed, and the successful one
took 90 seconds against 34 for an ordinary page. The handler was raising CancelledError,
because Playwright's SYNCHRONOUS api cannot be called re-entrantly from inside one of its own
event callbacks -- every send() from the handler was a nested call into the event loop already
running it. page.route() is the sync api's own answer to this question, it hands the handler a
route object with abort() and continue_(), and it knows each request's resource type without
being told.

EXPERIMENTAL, AND OFF UNTIL MEASURED. MCP_CAPTURE_LEAN=1 turns it on. The first live trial
measured NO saving at all -- 108.8 MB against 109.0 -- but on a browser busy with a fleet run,
at n=2, through the broken interception above. That number is not yet evidence either way.
"""
from __future__ import annotations

import os
import threading

#: The resource types that carry bytes and take part in no protocol. Playwright spells them
#: lower case; CDP spells them capitalised, and the first version used the CDP spelling with
#: page.route, where a misspelt type silently matches nothing.
#: STYLESHEET IS NOT IN THE SET, AND THAT WAS MEASURED. The first live trial blocked all four
#: and the composer never rendered at all -- three captures in three, then two in two, each
#: after the opener had spent its full 75 seconds waiting for a page that was never going to
#: finish. scripts/win/lean_capture_isolate.py answers which of the four does that without
#: spending a Copilot turn, by opening the page under each candidate set and looking for the
#: composer:
#:
#:     (no blocking)                 composer=True    4.2s
#:     image                         composer=True    9.3s   blocked 23
#:     font                          composer=True    7.5s   blocked  0
#:     media                         composer=True    7.5s   blocked  0
#:     stylesheet                    composer=False  42.9s   blocked 47
#:     image,font,media              composer=True    6.3s   blocked 18
#:     image,font,media,stylesheet   composer=False  42.6s   blocked 71
#:
#: So the app waits for its CSS, and blocking it does not make a lighter page -- it makes a
#: page that never finishes. Font and media are kept although they blocked NOTHING on this
#: surface: they cost nothing to leave in, and another agent surface may serve them.
#:
#: OVERRIDABLE because the set has already had to be narrowed once by measurement, and the
#: next surface may narrow it again.
BLOCKED_TYPES = tuple(
    t.strip().lower() for t in
    os.environ.get("MCP_CAPTURE_LEAN_TYPES", "image,font,media").split(",")
    if t.strip())

#: How many handler errors are tolerated before interception is torn down. A handler that has
#: started failing must not be allowed to hold requests: the page would hang until the capture
#: timeout, and a hung capture blocks the fleet's main loop.
ERROR_BUDGET = 5


def enabled() -> bool:
    return os.environ.get("MCP_CAPTURE_LEAN", "0").strip().lower() not in (
        "0", "false", "no", "off", "")


_LEAN = threading.local()


class _Interception:
    """One page's route handler, with the teardown that must happen on every exit path."""

    def __init__(self, page):
        self.page = page
        self.blocked = 0
        self.allowed = 0
        self.errors = 0
        self.torn_down = False

    def handle(self, route):
        """NEVER RAISES. An exception here leaves the request unanswered and the page waiting
        on it; the capture then burns its whole timeout and a real turn with it."""
        try:
            kind = (route.request.resource_type or "").lower()
        except Exception:
            kind = ""
        try:
            if kind in BLOCKED_TYPES and not self.torn_down:
                route.abort()
                self.blocked += 1
            else:
                # FALLBACK, NOT CONTINUE. continue_() RE-ISSUES the request from Playwright,
                # which rewrites it and changes its timing; fallback() hands it back to the
                # browser to perform as it normally would. With continue_() on every request
                # the composer never rendered at all -- three captures in three failed with
                # "conversation tab/composer is closed", each after the opener had spent its
                # full 75 seconds waiting for a page that was never going to finish.
                route.fallback()
                self.allowed += 1
        except Exception:
            self.errors += 1
            try:
                route.fallback()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
            if self.errors >= ERROR_BUDGET:
                # Stop interfering rather than keep failing. Unrouting releases the page to
                # load as an ordinary one.
                self.teardown()

    def teardown(self):
        """Idempotent, and called on every exit -- success, timeout, cancellation, crash."""
        if self.torn_down:
            return
        self.torn_down = True
        try:
            self.page.unroute("**/*", self.handle)
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
        interception = _Interception(page)
        page.route("**/*", interception.handle)
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
