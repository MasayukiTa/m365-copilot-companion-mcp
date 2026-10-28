"""A single keep-alive blank page, reused rather than re-opened.

Regola: 2026-09-10. 行#243 -- "いまaboutblankが開かれているのこれ問題だとは思わないの?"

Two cleanup paths (fleet_runner._close_idle_copilot_pages at START, and
run_relay_fleet's end-of-run sweep in relay_fleet) close every stale Copilot
page. When those are the only pages left, each used to open a fresh
``about:blank`` first so Edge would not exit with its last page -- but each did
it UNCONDITIONALLY. A browser the operator has surfaced (headed, during sign-in)
then shows a stray blank tab, and a restart that opens a keep-alive without
reusing an existing blank accumulates ``about:blank`` tabs one per sweep.

The bridge already guards this (copilot_bridge: ``if not any((pg.url or "") in
("about:blank", "") ...)``). The fleet did not. This helper carries that one
rule for both fleet call sites: if a blank page already exists, REUSE it and
open nothing; only open one when there is none.

Kept dependency-free and side-effect-only on the context so it can be unit
tested without a browser.
"""
from __future__ import annotations

#: A page whose URL is one of these is already a usable keep-alive: opening
#: another would only add a second stray blank tab.
_BLANK_URLS = ("about:blank", "")


def _is_blank(page) -> bool:
    """True when this page is an existing blank/keep-alive that can be reused."""
    try:
        url = page.url or ""
    except Exception:
        return False
    return url in _BLANK_URLS


def existing_blank(context):
    """Return a blank page already open on the context, or None.

    Never raises: a context that cannot be inspected yields None, and the caller
    falls back to opening one (its own try/except guards that).
    """
    try:
        pages = list(context.pages)
    except Exception:
        return None
    for pg in pages:
        if _is_blank(pg):
            return pg
    return None


def ensure_keepalive_blank(context, timeout_ms: int = 10000):
    """Guarantee one keep-alive blank page WITHOUT opening a second.

    REUSE over re-open (行#243): if the context already has an ``about:blank``
    (or empty-URL) page, return it and open nothing -- so a sweep cannot leave a
    second stray blank tab behind, and a surfaced browser does not sprout one.
    Only when there is none is a single new blank page opened.

    Returns the page on success, or None when a fresh page could not be opened
    (the caller treats None as "no keep-alive -> do not risk closing the
    browser").
    """
    page = existing_blank(context)
    if page is not None:
        return page
    try:
        page = context.new_page()
        page.goto("about:blank", timeout=timeout_ms)
        return page
    except Exception:
        return None
