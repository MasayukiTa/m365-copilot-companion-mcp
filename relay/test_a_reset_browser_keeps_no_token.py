# -*- coding: utf-8 -*-
"""The reset docstring said the token was gone. The token was not gone.

`relay/relay_fleet.py::reset_socket_route` is called after an Edge hard reset and its docstring
ends:

    A new browser is a new route. Nothing is preserved: not the token, which belongs to the
    context that just died, and not the failure counters, which describe a browser that no
    longer exists.

MEASURED 2026-09-13: it cleared `_SOCKET_ROUTE` and the fault clock and nothing else.
`relay/profile_token.py` keeps its own module-level `_MEMO` of the last answer per agent
surface, and `_memo_get` serves it back whenever the entry is inside `MIN_CAPTURE_INTERVAL_S`
and the token still has `SERVE_FLOOR_S` of life. So the first send after a hard reset could be
handed a token minted against a browser context that no longer exists.

`profile_token.forget_memo` exists for exactly this -- "For tests, and for a browser that was
reset underneath us" -- and had no caller.

NOT FATAL, AND THAT IS WHY IT SURVIVED. The light path fails and falls through to a fresh
capture, so the cost is a wasted attempt rather than a lost run -- which is precisely how a
docstring came to assert something the code did not do for long enough to be quoted.
"""
from __future__ import annotations

import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.profile_token as PT   # noqa: E402
import relay.relay_fleet as RF     # noqa: E402


def _seed_memo(monkeypatch, url="http://agent/x"):
    """A memo entry that `_memo_get` would serve: recent, and with life left."""
    monkeypatch.setattr(PT, "_MEMO", {}, raising=False)
    monkeypatch.setattr(PT, "_WARNED", set(), raising=False)
    PT._MEMO[url] = {"token": "tok", "template": {"a": 1}, "at": time.time()}
    return url


# ── the defect ────────────────────────────────────────────────────────────────────────────

def test_a_hard_reset_drops_the_cached_token(monkeypatch):
    url = _seed_memo(monkeypatch)
    assert PT._MEMO.get(url), "前提が崩れている: memo を仕込めていない"
    RF.reset_socket_route()
    assert not PT._MEMO, (
        "ブラウザを作り直したのに、死んだコンテキストで発行されたトークンが残っている")


def test_the_route_and_the_fault_clock_are_still_cleared(monkeypatch):
    """The new call must not have displaced what the function already did."""
    monkeypatch.setattr(RF, "_SOCKET_ROUTE", object(), raising=False)
    RF._LAST_ROUTE_FAULT[0] = 12345.0
    RF.reset_socket_route()
    assert RF._SOCKET_ROUTE is None
    assert RF._LAST_ROUTE_FAULT[0] == 0.0


def test_a_failure_to_clear_the_memo_does_not_break_the_reset(monkeypatch):
    """The reset runs on the recovery path, after a browser has already died. Raising here
    would turn a recoverable reset into a lost run -- a worse trade than a stale memo."""
    def _boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(PT, "forget_memo", _boom)
    monkeypatch.setattr(RF, "_SOCKET_ROUTE", object(), raising=False)
    RF.reset_socket_route()
    assert RF._SOCKET_ROUTE is None


# ── the claim the docstring makes ─────────────────────────────────────────────────────────

def test_the_docstring_still_says_what_the_code_now_does():
    """It asserted "not the token" while the token survived. If the clearing is ever removed,
    the sentence becomes false again -- so the sentence and the call are pinned together."""
    import inspect

    src = inspect.getsource(RF.reset_socket_route)
    assert "not the token" in src, "この主張が消えたなら、この検査ごと導出し直すこと"
    assert "forget_memo()" in src, "主張だけ残って実装が消えている"
