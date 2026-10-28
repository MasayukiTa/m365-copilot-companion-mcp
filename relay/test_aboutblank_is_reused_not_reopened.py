"""A keep-alive blank tab is REUSED, never opened a second time (行#243).

Regola: 2026-09-10. 行#243 -- "いまaboutblankが開かれているのこれ問題だとは思わないの?"

Two fleet cleanup paths open a keep-alive ``about:blank`` before closing the
last stale Copilot page so Edge will not exit with its final page. Each used to
open one UNCONDITIONALLY: a browser the operator had surfaced showed a stray
blank tab, and a restart that opened a keeper without reusing an existing blank
accumulated about:blank tabs one per sweep.

These tests pin the fix:
  * the shared helper reuses an existing blank and opens nothing;
  * it opens exactly one when there is none;
  * neither fleet call site opens about:blank directly any more -- both route
    through relay.keepalive.ensure_keepalive_blank.

Source-level assertions run in CI with no Edge present.
"""
from __future__ import annotations

import pathlib

from relay.keepalive import ensure_keepalive_blank, existing_blank

REPO = pathlib.Path(__file__).resolve().parents[1]


class _Pg:
    def __init__(self, url="about:blank"):
        self.url = url
        self.goals = []

    def goto(self, url, **kw):
        self.goals.append(url)
        self.url = url


class _Ctx:
    def __init__(self, urls=()):
        self.pages = [_Pg(u) for u in urls]
        self.opened = 0

    def new_page(self):
        self.opened += 1
        p = _Pg("")
        self.pages.append(p)
        return p


def test_reuses_an_existing_blank_and_opens_nothing():
    ctx = _Ctx(["https://m365.cloud.microsoft/chat", "about:blank"])
    before = len(ctx.pages)
    page = ensure_keepalive_blank(ctx)
    assert ctx.opened == 0, "a blank already existed; opening another leaks a stray tab"
    assert len(ctx.pages) == before, "page count must not grow when a blank is reused"
    assert page is not None and page.url == "about:blank"


def test_reuses_an_empty_url_page_too():
    # Edge reports a just-created keeper as an empty URL before it settles.
    ctx = _Ctx([""])
    page = ensure_keepalive_blank(ctx)
    assert ctx.opened == 0
    assert page is ctx.pages[0]


def test_opens_exactly_one_when_there_is_none():
    ctx = _Ctx(["https://m365.cloud.microsoft/chat"])
    page = ensure_keepalive_blank(ctx)
    assert ctx.opened == 1, "with no blank present, open exactly one keeper"
    assert page is not None
    assert "about:blank" in page.goals


def test_a_second_call_does_not_open_a_second_blank():
    # The accumulation bug: sweep after sweep must not add tabs.
    ctx = _Ctx(["https://m365.cloud.microsoft/chat"])
    ensure_keepalive_blank(ctx)
    ensure_keepalive_blank(ctx)
    ensure_keepalive_blank(ctx)
    assert ctx.opened == 1, "repeated sweeps must reuse the one keeper, never stack blanks"


def test_existing_blank_returns_none_when_all_pages_are_real():
    ctx = _Ctx(["https://m365.cloud.microsoft/chat",
                "https://m365.cloud.microsoft/other"])
    assert existing_blank(ctx) is None


def test_helper_never_raises_on_a_bad_context():
    class _Bad:
        @property
        def pages(self):
            raise RuntimeError("context gone")

        def new_page(self):
            raise RuntimeError("context gone")

    assert ensure_keepalive_blank(_Bad()) is None


# ---------------------------------------------------------------------------
# Source-level: neither fleet call site opens about:blank directly any more.
# ---------------------------------------------------------------------------

_DIRECT = 'new_page().goto("about:blank"'


def test_fleet_runner_routes_through_the_helper():
    body = (REPO / "relay" / "fleet_runner.py").read_text(encoding="utf-8", errors="replace")
    assert _DIRECT not in body, "fleet_runner still opens about:blank directly"
    assert "ensure_keepalive_blank" in body, "fleet_runner must route through the keeper helper"


def test_relay_fleet_routes_through_the_helper():
    body = (REPO / "relay" / "relay_fleet.py").read_text(encoding="utf-8", errors="replace")
    assert _DIRECT not in body, "relay_fleet still opens about:blank directly"
    assert "ensure_keepalive_blank" in body, "relay_fleet must route through the keeper helper"
