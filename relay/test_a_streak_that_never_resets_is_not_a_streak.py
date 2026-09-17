# -*- coding: utf-8 -*-
"""The unlanded-call counter says "連続" to the operator. This is what makes that word true.

FOUND BY READING, NOT BY A FAILURE. relay/relay_fleet.py filed a worker INFRA_STUCK once
`_unlanded_calls` reached 2, and nothing anywhere ever set it back to 0. A worker that wrote a
stray invocation on turn 3, recovered, and wrote another on turn 40 was therefore stopped with
"ツール呼び出しが2 ターン連続でゲートウェイに到達していない" -- a sentence describing something
that did not happen, handed to a person who would act on it. The counter was right about the
total and wrong about the claim it was printed inside.

The reset lives above the FAIL / steer / continue branch rather than inside one of them,
because the turn that breaks the streak is any turn whose reply carries no unlanded
invocation, whatever branch that reply then takes. Putting it in a branch is how one path
quietly keeps a stale count.
"""
from __future__ import annotations

import pytest

import relay.relay_fleet as F

MARKUP = 'つぎを実行します <invoke name="x-call_tool"> <parameter name="name">glob'
PLAIN = "ウィンドウを6件確認しました。次に進みます。"


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    return F.RelayWorker("テスト用のゴール", "w0")


def _decide(w, resp):
    """_decide does much more than the branch under test; only the counter is asserted on."""
    try:
        w._decide(resp)
    except Exception:
        pass


def test_a_clean_turn_clears_the_streak(worker):
    """THE DEFECT. One stray invocation, then a normal reply -- the streak is over, and the
    next stray one must not be reported as the second of two consecutive."""
    _decide(worker, MARKUP)
    assert getattr(worker, "_unlanded_calls", 0) == 1
    _decide(worker, PLAIN)
    assert worker._unlanded_calls == 0, "a reply with no unlanded call ended the streak"
    _decide(worker, MARKUP)
    assert worker._unlanded_calls == 1, "counted as the first again, not the second"


def test_two_in_a_row_still_stops_the_worker(worker):
    """The other half: the reset must not disarm the detector it guards. Two consecutive
    replies that both write out an invocation are the case it exists for."""
    _decide(worker, MARKUP)
    _decide(worker, MARKUP)
    assert worker._unlanded_calls >= 2
    assert worker.outcome == "INFRA_STUCK"
    assert "連続" in (worker.reason or "")


def test_the_landing_check_is_gone_and_stays_gone():
    """A helper asking "did ANY call land in this window" stood in relay_fleet with a passing
    test and no caller. It reads as the obvious companion to the markup test, and it is the
    design that was already measured dead: on the run it was written for, 19 calls landed in
    the window and 18 succeeded, so requiring zero made the branch unreachable. This asserts
    the absence so the next reader reaches for the comment instead of rewriting it."""
    assert not hasattr(F, "_no_tool_call_landed")
