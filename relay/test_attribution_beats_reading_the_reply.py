# -*- coding: utf-8 -*-
"""When a refusal can only have been this worker's, no rule about prose is needed.

THE PROBLEM. The relay decided "this worker was refused" by pattern-matching the worker's
REPLY TEXT for the server's refusal strings plus a length rule. The server records every
refusal with an Mcp-Session-Id, but the relay has no mapping from a worker to the session the
connector used, so it read prose instead. That heuristic has misfired in both directions: a
533-character meeting summary was classified as a lock error because some other concurrent
worker had been refused in the window, and a worker writing 「no valid unlock token で拒否」
was missed because the bracket was absent and its replies ran past the length cap.

WHAT WAS MEASURED BEFORE BUILDING ANY OF THIS, over 39 real runs: attributing a session to
the worker whose turn window contains all its calls resolves uniquely for 100% of sessions at
2 concurrent workers, 31% at 8, 15% at 15, 3% at 96 -- overall 45 of 470, 10%. So an exact
join is NOT available in general, and a design that assumed one would be a 10% signal
presented as certainty. astra reached the same conclusion independently: an exact join for
every event needs a reliable identifying signal on every request, or guaranteed isolation.

SO THE CLAIM HERE IS DELIBERATELY NARROW. Not "we can attribute refusals" -- we cannot, in
general. Only: when exactly one worker had a turn in flight at the refusal's timestamp, the
refusal is that worker's, and that case needs no prose. Everything else falls through to the
rules that were already there, unchanged.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import relay_fleet as F  # noqa: E402
from relay import turn_windows as TW  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    TW.reset()
    yield
    TW.reset()


# ---- the registry's own arithmetic -------------------------------------------------------

def test_one_worker_in_flight_owns_the_moment():
    TW.open_turn("w0", 100.0)
    assert TW.exclusive_owner(105.0) == "w0"
    assert TW.belongs_to("w0", 105.0)


def test_two_workers_in_flight_owns_nothing():
    """THE CASE THAT MAKES THIS HONEST. Both are candidates, so neither is the answer."""
    TW.open_turn("w0", 100.0)
    TW.open_turn("w1", 101.0)
    assert TW.candidates(105.0) == ["w0", "w1"]
    assert TW.exclusive_owner(105.0) is None
    assert not TW.belongs_to("w0", 105.0)
    assert not TW.belongs_to("w1", 105.0)


def test_nobody_in_flight_owns_nothing():
    assert TW.exclusive_owner(105.0) is None
    TW.open_turn("w0", 200.0)
    assert not TW.belongs_to("w0", 105.0), "an event before the turn started was claimed"


def test_a_closed_window_stops_claiming_once_the_grace_expires():
    """A refusal is written just before the reply returns, so a closed window must still
    answer for a moment -- but a worker that finished long ago must not keep claiming."""
    TW.open_turn("w0", 100.0)
    TW.close_turn("w0", 110.0)
    assert TW.belongs_to("w0", 110.0 + TW.GRACE_S - 1)
    assert not TW.belongs_to("w0", 110.0 + TW.GRACE_S + 1)


def test_an_open_window_claims_from_its_start_but_not_forever():
    """No end yet means the turn is still running, so anything since its start is a
    candidate -- bounded, because an open window left behind by a dead worker would otherwise
    claim every later event, and "exclusively this worker's" about an event nobody was there
    for is the wrong answer in its most confident form."""
    TW.open_turn("w0", 100.0)
    assert TW.belongs_to("w0", 100.0)
    assert TW.belongs_to("w0", 100.0 + TW.MAX_OPEN_S - 1)
    assert not TW.belongs_to("w0", 100.0 + TW.MAX_OPEN_S + 1)


def test_forget_removes_a_worker():
    TW.open_turn("w0", 100.0)
    TW.forget("w0")
    assert TW.exclusive_owner(105.0) is None


# ---- the branch in the relay -------------------------------------------------------------

def _refusal(ts, detail="[locked: no valid unlock token for 'x'] ..."):
    return {"ts": ts, "detail": detail, "session": "s1", "event": "refused"}


@pytest.fixture
def refusals(monkeypatch):
    """Stand in for the server's refusal ledger."""
    def _use(records):
        import tools.lock_state as ls
        monkeypatch.setattr(ls, "matching_records", lambda since, now=None: list(records))
    return _use


def test_an_exclusive_refusal_is_detected_with_no_marker_in_the_reply(refusals):
    """The whole point: the reply says nothing recognisable and the verdict is still right."""
    TW.open_turn("w0", 100.0)
    refusals([_refusal(105.0)])
    reply = "ファイルを3件確認しました。次に進みます。" * 40   # long, and no marker
    assert F._looks_locked(reply, since=100.0, worker="w0")


def test_it_declines_when_another_worker_could_own_the_refusal(refusals):
    """Two in flight: attribution is not established, so this branch must not fire. The
    reply carries nothing either, so the whole function must say no."""
    TW.open_turn("w0", 100.0)
    TW.open_turn("w1", 100.0)
    refusals([_refusal(105.0)])
    reply = "ファイルを3件確認しました。次に進みます。" * 40
    assert not F._looks_locked(reply, since=100.0, worker="w0")


def test_the_old_marker_rule_still_fires_when_attribution_is_unavailable(refusals):
    """Nothing is regressed for the cases the prose rules always covered."""
    refusals([])
    reply = "[locked: no valid unlock token for '10.0.0.1'] call unlock(password=...)"
    assert F._looks_locked(reply, since=100.0, worker="w0")


def test_a_no_context_refusal_is_not_attributed(refusals):
    """An in-process call is not this worker's HTTP turn, however exclusive the moment."""
    TW.open_turn("w0", 100.0)
    refusals([_refusal(105.0, detail=F.NO_CONTEXT_REFUSAL + " denied ...")])
    assert not F._exclusively_refused("w0", 100.0)


def test_a_refusal_before_the_turn_started_is_not_this_turns(refusals):
    TW.open_turn("w0", 200.0)
    refusals([_refusal(105.0)])
    assert not F._exclusively_refused("w0", 200.0)


def test_no_worker_name_means_no_attribution(refusals):
    """Callers that cannot say who they are get the old behaviour, not a wrong answer."""
    TW.open_turn("w0", 100.0)
    refusals([_refusal(105.0)])
    assert not F._exclusively_refused("", 100.0)


def test_an_abandoned_open_window_stops_claiming():
    """An open window claims everything since it started, which is right while a turn is
    really in flight and wrong once nothing is driving it -- a dead worker, a crashed
    coordinator, a test that opened and never closed. Saying "exclusively this worker's"
    about an event nobody was there for is the wrong answer in the most confident form."""
    TW.open_turn("w0", 1000.0)
    assert TW.belongs_to("w0", 1500.0)
    assert not TW.belongs_to("w0", 1000.0 + TW.MAX_OPEN_S + 60)


def test_the_window_is_closed_when_the_reply_arrives(monkeypatch):
    """Without this the window stays open and the worker keeps claiming later events -- and
    while it is the only worker running, exclusivity would hold spuriously for as long as the
    open-window bound allows. _decide is the moment the reply exists, so it closes there."""
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    w = F.RelayWorker("テスト用のゴール", "w0")
    TW.open_turn("w0", 100.0)
    assert TW.snapshot()["w0"][1] is None, "the fixture did not leave an open window"
    try:
        w._decide("ふつうの返答です。")
    except Exception:
        pass          # _decide does much more than this; only the close is under test
    assert TW.snapshot()["w0"][1] is not None, "the window was left open after the reply"
