# -*- coding: utf-8 -*-
"""Five turns gave up at 240s against a 1200s budget, and every record named the wrong clock.

MEASURED on run `r6aa597a8_a0` (2026-09-13 03:19→04:07), read out of its own transcript:

    turn 2  timeout=241.3  origin=socket_turn  budget_s=240  treatment=retry  transient=1
    turn 3  timeout=240.5  origin=socket_turn  budget_s=240  treatment=retry  transient=2
    turn 4  timeout=240.9  ...                                                transient=3
    turn 5  timeout=240.6  ...                                                transient=4
    turn 6  timeout=240.6  ...                                                transient=5

`SOCKET_TURN_TIMEOUT_S` is 1200. Every one of those rows says a 1200-second clock expired at
240, which is precisely the reading the `origin` field exists to prevent -- its own docstring:
*"Without it an inner overrun reads as an outer one."*

TWO DEFECTS IN ONE BRANCH.

1. The comparison was always `self.per_turn_timeout_s`, the tab-era budget. `_defer_generation`
   three hundred lines above already carries the correction, in its own words: *"a socket turn
   is bounded by its own turn_timeout_s and _defer_generation deliberately skips this tab-era
   budget for it -- but the line still printed the skipped number... Correct behaviour,
   reported as a breach."* The timeout branch never got the same treatment.
2. `_origin` was chosen from `self.socket` -- the worker's TRANSPORT -- not from the clock that
   fired, and `_note_timeout` recorded `budget_s=self.per_turn_timeout_s` whichever clock it
   was. So the label and the number could not disagree with each other even when both were
   wrong.

WHAT IT COST. Turn 3's reply arrived at 03:34:14, seventy seconds into the turn. The turn was
declared timed out at 03:37:04 and the full 7,890-character goal was re-sent three more times.
1,204 seconds -- twenty of the forty-eight minutes -- went to a budget that was not the one the
worker was supposed to be running under.

NOT A NEW POLICY. The bound was already declared (`SOCKET_TURN_TIMEOUT_S`, passed to the driver
at three call sites) and already applied on the generation-wait path. This makes the timeout
branch agree with the rest of the file rather than choosing a number.
"""
from __future__ import annotations

import io
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as RF  # noqa: E402


class _Tx:
    def __init__(self):
        self.rows = []

    def metric(self, turn, name, value, **kw):
        self.rows.append(dict(kw, turn=turn, name=name, value=value))


def _worker(socket):
    w = RF.RelayWorker("ある用事", "w0", fanout=False)
    w.socket = socket
    w._tx = _Tx()
    return w


# ── the record names the clock that fired ─────────────────────────────────────────────────

def test_a_socket_row_carries_the_socket_budget():
    """THE DEFECT. Every row said budget_s=240 for a bound of 1200."""
    w = _worker(socket=True)
    w._note_timeout("socket_turn", 1201.4, "retry", budget_s=RF.SOCKET_TURN_TIMEOUT_S)
    row = w._tx.rows[0]
    assert row["origin"] == "socket_turn"
    assert row["budget_s"] == RF.SOCKET_TURN_TIMEOUT_S
    assert row["budget_s"] != w.per_turn_timeout_s


def test_a_tab_row_still_carries_the_tab_budget():
    w = _worker(socket=False)
    w._note_timeout("per_turn", 241.0, "retry", budget_s=w.per_turn_timeout_s)
    assert w._tx.rows[0]["budget_s"] == w.per_turn_timeout_s


def test_an_omitted_budget_falls_back_to_the_old_field():
    """Other callers may exist; none should learn a new required argument from this fix."""
    w = _worker(socket=True)
    w._note_timeout("socket_turn", 5.0, "retry")
    assert w._tx.rows[0]["budget_s"] == w.per_turn_timeout_s


def test_recording_a_timeout_cannot_raise():
    """It runs beside a failure path, and a failure path that can fail again is worse than no
    record. Its own docstring says so."""
    w = _worker(socket=True)
    w._tx = None
    w._note_timeout("socket_turn", 1.0, "retry", budget_s=1200)
    assert True  # reaching here is the assertion


# ── the branch applies the matching bound ─────────────────────────────────────────────────

def _source_of(fn_name):
    src = io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("    def %s(" % fn_name)
    return src[i:src.index("\n    def ", i + 10)]


def test_the_waiting_branch_no_longer_compares_against_the_tab_budget():
    """Asserted on the branch rather than by running a live poll loop, which needs a browser.
    Scoped to `poll`, so a mention elsewhere cannot satisfy it."""
    body = _source_of("poll")
    assert "> _bound:" in body, "境界を _bound から取っていない"
    assert "self._t_send > self.per_turn_timeout_s" not in body, (
        "socket ワーカーにタブ時代の予算を当てる比較が戻っている")
    assert "SOCKET_TURN_TIMEOUT_S if getattr(self, \"socket\", False)" in body


def test_the_socket_bound_is_longer_than_the_tab_one():
    """If these ever cross, the fix above silently becomes a no-op and the run that motivated
    it would look fine in the record."""
    w = _worker(socket=True)
    assert RF.SOCKET_TURN_TIMEOUT_S > w.per_turn_timeout_s, (
        RF.SOCKET_TURN_TIMEOUT_S, w.per_turn_timeout_s)


# ── against the run it was found in ───────────────────────────────────────────────────────

TRANSCRIPT = os.path.join(REPO, ".fleet", "transcripts", "r6aa597a8_a0_w0.jsonl")


def test_the_run_that_paid_for_this_is_still_readable():
    """A fixture proves what I think happened; the transcript proves what did. Skipped once
    retention takes it, rather than quietly passing on nothing."""
    import json

    if not os.path.isfile(TRANSCRIPT):
        pytest.skip("the transcript for r6aa597a8_a0 is no longer on this machine")
    rows = [json.loads(l) for l in io.open(TRANSCRIPT, encoding="utf-8", errors="replace")
            if l.strip()]
    outs = [r for r in rows if r.get("name") == "timeout"]
    assert len(outs) == 5, len(outs)
    # Every one of them: a 1200s clock, recorded as having expired at 240.
    assert all(r["origin"] == "socket_turn" for r in outs)
    assert all(r["budget_s"] == 240 for r in outs)
    assert all(230 < r["value"] < 250 for r in outs), [r["value"] for r in outs]

    # And the answer that was already in hand when the third one fired.
    a3 = [r for r in rows if r.get("role") == "assistant" and r.get("turn") == 3][0]
    m3 = [r for r in outs if r.get("turn") == 3][0]
    assert a3["ts"] < m3["ts"], "前提が崩れている: 返答は timeout より前に届いていたはず"
    assert m3["ts"] - a3["ts"] > 120, round(m3["ts"] - a3["ts"])
    assert time.gmtime(a3["ts"]) is not None
