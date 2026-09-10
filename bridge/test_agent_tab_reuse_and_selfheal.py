# -*- coding: utf-8 -*-
"""The self-heal for duplicate agent tabs is disarmed by the same predicate that fails.

MEASURED INCIDENT, 2026-09-10. CDP :9223 (the bridge's HEADLESS Edge, profile
copilot-bridge-edge) held 71 pages, 69 of them on one identical URL --
https://m365.cloud.microsoft/chat/?titleId=T_02140b8c-... , which is exactly
MCP_IMPL_AGENT_URL. At 16:17 there was 1 page; by 18:54 there were 71. Nobody opened them by
hand: the browser has no window.

Over the same hours bridge.log recorded `_find_or_open_agent: no reusable agent tab found --
opened a new one` 37 times. Both facts at once are the tell: tabs that ARE on the agent surface
were not recognised as such, so

  1. _find_or_open_agent could not reuse one and opened another, and
  2. _close_duplicate_agent_tabs -- which exists precisely to clean these up -- recognised none
     of them and closed nothing.

One predicate, two symptoms, and the second is the dangerous one: the self-heal is guarded by
the very check that fails, so the more tabs accumulate the less able it is to remove any. In a
headless browser holding scores of tabs, most renderers are discarded, and a discarded page
answers 0 to locator().count() -- so the composer check fails on a tab that is otherwise a
perfectly good agent tab.

These tests reproduce that with a fake context: no browser, no CDP, no network.
"""
from __future__ import annotations

import os
import sys

BRIDGE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BRIDGE))

from bridge import copilot_bridge as B  # noqa: E402

AGENT_URL = "https://m365.cloud.microsoft/chat/?titleId=T_02140b8c-f551-675b-516a-4c7d2b08867e"


class _Locator:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class _FakePage:
    """A page whose composer may or may not answer -- which is the whole variable here."""

    def __init__(self, url, composer=1, name=""):
        self.url = url
        self._composer = composer
        self.closed = False
        self.name = name or url

    def locator(self, _sel):
        return _Locator(self._composer)

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class _FakeCtx:
    def __init__(self, pages):
        self.pages = list(pages)


def test_a_settled_agent_tab_is_recognised():
    """The control: with the composer answering, the matcher works. If this fails the fixture
    is wrong, not the code."""
    pg = _FakePage(AGENT_URL, composer=1)
    assert B._agent_tab_matches(pg, AGENT_URL) is True


def test_a_discarded_agent_tab_is_not_recognised():
    """THE FAULT. Same URL, same surface, renderer discarded -- and the tab becomes invisible to
    both the reuse path and the self-heal."""
    pg = _FakePage(AGENT_URL, composer=0)
    assert B._agent_tab_matches(pg, AGENT_URL) is False, (
        "if this now passes, the matcher no longer depends on a live renderer and the "
        "accumulation this test pins can no longer happen")


def test_the_self_heal_closes_discarded_orphans():
    """THE INCIDENT, now pinned as fixed. Before the fix this same fixture left all 69 open.

    _close_duplicate_agent_tabs was gated on _agent_tab_matches, which requires a live composer
    -- the right question for handing a tab to a conversation, and exactly the wrong one for
    deciding what to sweep up. A headless browser holding scores of tabs discards their
    renderers, so the tabs most in need of closing were the only ones the cleaner could not
    see. It now decides on the URL (see _agent_tab_url_matches).
    """
    doomed = [_FakePage(AGENT_URL, composer=0, name="leaked-%d" % i) for i in range(69)]
    keep = _FakePage(AGENT_URL, composer=1, name="keep")
    ctx = _FakeCtx(doomed + [keep])

    B._close_duplicate_agent_tabs(ctx, keep, AGENT_URL)

    assert all(p.closed for p in doomed), (
        "the cleaner is blind to discarded tabs again; 69 of them accumulated on a headless "
        "Edge the last time this was true")
    assert keep.closed is False, "the tab it was told to keep must never be closed"


def test_the_self_heal_does_work_when_the_tabs_are_live():
    """Proof the cleaner itself is not broken -- only its gate is. With live renderers it closes
    every duplicate and keeps the one it was given."""
    doomed = [_FakePage(AGENT_URL, composer=1, name="dup-%d" % i) for i in range(5)]
    keep = _FakePage(AGENT_URL, composer=1, name="keep")
    ctx = _FakeCtx(doomed + [keep])

    B._close_duplicate_agent_tabs(ctx, keep, AGENT_URL)

    assert all(p.closed for p in doomed), "the cleaner failed on tabs it CAN see"
    assert keep.closed is False


def test_a_non_agent_tab_is_never_closed():
    """The guard that must survive any fix to the above: a blank keep-alive page, or the user's
    own tab, is not a duplicate. Edge exits with its last page, so closing the blank would take
    the browser down with it."""
    blank = _FakePage("about:blank", composer=0)
    other = _FakePage("https://example.com/", composer=1)
    keep = _FakePage(AGENT_URL, composer=1)
    ctx = _FakeCtx([blank, other, keep])

    B._close_duplicate_agent_tabs(ctx, keep, AGENT_URL)

    assert blank.closed is False, "the keep-alive blank was closed; Edge exits with its last page"
    assert other.closed is False, "a non-agent tab was closed"


# -- the producer: a failed reopen orphans the tab it just opened ------------------------------
#
# THE OTHER HALF OF THE SAME INCIDENT. In the 2.6 hours the 69 tabs accumulated, bridge.log
# recorded "agent page had closed and could not be reopened" 76 times. That message comes from
# ensure_page_alive's except branch, and the page _find_or_open_agent had already created inside
# the try is never closed there -- the reference is simply lost. 76 failures, 69 surviving tabs.
#
# _find_or_open_agent opens the tab BEFORE it navigates:
#     pg = ctx.new_page()
#     pg.goto(url, wait_until="domcontentloaded")
# so any failure in the navigation (or in anything after it) leaves a page on the agent URL with
# nobody holding it. Combined with the disarmed self-heal above, nothing can ever remove it.

class _FailingGotoPage(_FakePage):
    """A page that opens fine and then fails to navigate -- the shape of every one of those 76."""

    def __init__(self, url=AGENT_URL):
        _FakePage.__init__(self, url, composer=0)
        self.goto_calls = 0

    def goto(self, *_a, **_k):
        self.goto_calls += 1
        raise RuntimeError("net::ERR_TIMED_OUT at " + AGENT_URL)


class _OpeningCtx:
    """A context that records every page it hands out, so the test can ask what survived."""

    def __init__(self, existing=()):
        self.pages = list(existing)
        self.created = []

    def new_page(self):
        pg = _FailingGotoPage()
        self.created.append(pg)
        self.pages.append(pg)
        return pg


def test_a_failed_reopen_closes_the_page_it_opened(monkeypatch):
    """The producer, pinned as fixed. Before the fix this left one orphan per failure."""
    ctx = _OpeningCtx()
    monkeypatch.setattr(B, "CTX", ctx, raising=False)
    monkeypatch.setattr(B, "PAGE", None, raising=False)
    monkeypatch.setattr(B, "DRIVER", None, raising=False)
    monkeypatch.setenv("MCP_IMPL_AGENT_URL", AGENT_URL)

    ok = B.ensure_page_alive()

    assert ok is False, "a reopen whose navigation failed must not report success"
    assert len(ctx.created) == 1, "the fixture did not exercise the open path"
    orphan = ctx.created[0]
    assert orphan.goto_calls >= 1, "navigation was never attempted"
    assert orphan.closed is True, (
        "THE LEAK IS BACK: the tab opened inside a failed reopen was left open, and the only "
        "caller (ensure_page_alive) keeps no reference with which to close it later -- that is "
        "how 76 failed reopens became 69 live tabs on one URL")


def test_repeated_failures_accumulate_nothing(monkeypatch):
    """The observed shape, inverted: 76 failures used to mean 69 tabs. Now it means none."""
    ctx = _OpeningCtx()
    monkeypatch.setattr(B, "CTX", ctx, raising=False)
    monkeypatch.setattr(B, "DRIVER", None, raising=False)
    monkeypatch.setenv("MCP_IMPL_AGENT_URL", AGENT_URL)

    for _ in range(10):
        monkeypatch.setattr(B, "PAGE", None, raising=False)
        assert B.ensure_page_alive() is False

    assert len(ctx.created) == 10
    assert [p.closed for p in ctx.created] == [True] * 10, (
        "one orphan per failed reopen is exactly the leak that filled a headless browser with "
        "69 identical tabs; every attempt must clean up after itself")


# -- the probe must not borrow a page it does not need ----------------------------------------
#
# MEASURED 2026-09-10. With transport=socket and no resident page, _run_tool_probe still
# borrowed a page every MCP_TOOL_PROBE_SEC: 37 "opened a new one" lines in one day, and page
# counts on :9223 oscillating 1 -> 2 -> 1 across the 32-minute verification window, while every
# turn went over the socket and touched nothing on that tab. The composer gate in
# _do_tool_probe_turn had already been taught that a socket turn needs no DOM; the BORROW was
# left behind, so a page was opened to satisfy nothing.
#
# It is not only waste: every borrow runs _find_or_open_agent, and a failure in there is exactly
# what orphaned 69 tabs on this port. Not opening a page is the only way not to leak one.

class _RecordingExecutor:
    """Stands in for PAGE_EXECUTOR and records what the probe asked it to run."""

    def __init__(self):
        self.submitted = []

    def submit_bounded(self, _timeout, fn, *a, **k):
        self.submitted.append(getattr(fn, "__name__", repr(fn)))
        return (False, None)

    def submit(self, fn, *a, **k):
        self.submitted.append(getattr(fn, "__name__", repr(fn)))
        return None


class _SocketDriver:
    IS_SOCKET = True
    failed = ""

    def send(self, *_a, **_k):
        raise RuntimeError("stop here: the probe's turn is not what this test is about")


def _arm_probe(monkeypatch, on_socket):
    """Get _run_tool_probe past its idle guards with a recording executor in place."""
    ex = _RecordingExecutor()
    monkeypatch.setattr(B, "PAGE_EXECUTOR", ex, raising=False)
    monkeypatch.setattr(B, "PAGE", None, raising=False)
    monkeypatch.setattr(B, "DRIVER", _SocketDriver() if on_socket else None, raising=False)
    monkeypatch.setattr(B, "_on_socket", lambda: on_socket, raising=False)
    monkeypatch.setattr(B, "MCP_TOOL_PROBE_SEC", 600.0, raising=False)
    monkeypatch.setattr(B, "_LAST_USER_TURN_TS", 0.0, raising=False)
    monkeypatch.setattr(B.tool_probe, "record_probe", lambda *a, **k: None, raising=False)
    return ex


def test_the_probe_borrows_no_page_while_the_conversation_is_on_a_socket(monkeypatch):
    ex = _arm_probe(monkeypatch, on_socket=True)
    try:
        B._run_tool_probe()
    except Exception:
        pass                    # a later step failing is fine; the borrow is the subject
    assert "borrow_page" not in ex.submitted, (
        "the probe borrowed a page on the socket transport again; that is a tab opened to "
        "satisfy nothing, and every borrow runs _find_or_open_agent, which is what orphaned "
        "69 tabs")


def test_the_probe_still_borrows_a_page_on_the_page_transport(monkeypatch):
    """The other half: on the page transport the page IS the conversation, so it must borrow.
    Without this, the fix above would read as 'never borrow' and quietly break page mode."""
    ex = _arm_probe(monkeypatch, on_socket=False)
    try:
        B._run_tool_probe()
    except Exception:
        pass
    assert "borrow_page" in ex.submitted, (
        "the page transport has no page and did not ask for one; the probe cannot work")


# -- the instrument this incident was missing --------------------------------------------------
#
# Nothing on this machine had ever recorded a page count over time, so when 69 orphaned tabs were
# found, the onset could not be dated -- only "no record shows it before today". Process counts
# had been sampled repeatedly and were useless by construction: the pages were same-origin, so
# Chromium shared ~7 renderers between all 70 and the process count sat flat at 17 throughout.

class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    def __init__(self, status, body):
        self._status = status
        self._body = body
        self.requested = None

    def request(self, _method, path):
        self.requested = path

    def getresponse(self):
        return _FakeResp(self._status, self._body)

    def close(self):
        pass


def _targets(n_agent, n_blank=1):
    import json as _json
    rows = [{"type": "page", "url": AGENT_URL} for _ in range(n_agent)]
    rows += [{"type": "page", "url": "about:blank"} for _ in range(n_blank)]
    rows += [{"type": "service_worker", "url": "sw.js"}]     # must not be counted as a page
    return _json.dumps(rows).encode("utf-8")


def test_the_page_count_counts_pages_not_targets(monkeypatch):
    conn = _FakeConn(200, _targets(69))
    monkeypatch.setattr(B.http.client, "HTTPConnection", lambda *a, **k: conn)
    total, agent = B.count_pages("http://127.0.0.1:9223")
    assert (total, agent) == (70, 69), (total, agent)
    assert conn.requested == "/json/list"


def test_an_unreadable_endpoint_reports_nothing_rather_than_zero(monkeypatch):
    """Zero pages and 'could not ask' must never be the same answer: a browser that has gone
    away would otherwise read as a browser holding no tabs, which is the shape of every
    green-by-omission failure in this project."""
    def _boom(*_a, **_k):
        raise OSError("connection refused")
    monkeypatch.setattr(B.http.client, "HTTPConnection", _boom)
    assert B.count_pages("http://127.0.0.1:9223") == (None, None)


def test_a_climbing_page_count_warns_once_per_new_high(monkeypatch, tmp_path):
    """It must shout, and it must not shout every minute forever -- a bridge legitimately
    holding a few pages would drown the log and the next real warning with it."""
    seen = []
    monkeypatch.setattr(B.logger, "warning", lambda m, *a: seen.append(m % a if a else m))
    monkeypatch.setattr(B, "PAGE_COUNT_LOG", str(tmp_path / "page_counts.jsonl"), raising=False)
    monkeypatch.setattr(B, "PAGE_COUNT_SAMPLE_SEC", 0.0, raising=False)
    monkeypatch.setattr(B, "PAGE_COUNT_WARN_AT", 8, raising=False)
    monkeypatch.setattr(B, "_PAGE_COUNT_LAST_SAMPLE", 0.0, raising=False)
    monkeypatch.setattr(B, "_PAGE_COUNT_LAST_WARNED", 0, raising=False)

    counts = [4, 12, 12, 40, 2]
    def _count(_cdp, timeout=4.0):
        n = counts.pop(0)
        return n, n - 1
    monkeypatch.setattr(B, "count_pages", _count)

    for _ in range(5):
        B.sample_page_count("http://127.0.0.1:9223")

    assert len(seen) == 2, ("expected a warning for 12 and for the new high 40, got: %s" % seen)
    assert "12 pages" in seen[0] and "40 pages" in seen[1]

    rows = [l for l in open(str(tmp_path / "page_counts.jsonl"), encoding="utf-8") if l.strip()]
    assert len(rows) == 5, "every sample must be persisted, warned about or not -- that history "
    import json as _json
    assert _json.loads(rows[3])["pages"] == 40


def test_the_history_is_bounded(monkeypatch, tmp_path):
    log = tmp_path / "page_counts.jsonl"
    log.write_text("\n".join(['{"ts": %d}' % i for i in range(50)]) + "\n", encoding="utf-8")
    monkeypatch.setattr(B, "PAGE_COUNT_LOG", str(log), raising=False)
    monkeypatch.setattr(B, "PAGE_COUNT_LOG_MAX_LINES", 10, raising=False)
    B._trim_page_count_log()
    rows = [l for l in open(str(log), encoding="utf-8") if l.strip()]
    assert len(rows) == 10, len(rows)
    assert '"ts": 49' in rows[-1], "trimming kept the wrong end; the newest samples must survive"
