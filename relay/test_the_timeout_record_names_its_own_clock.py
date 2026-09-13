# -*- coding: utf-8 -*-
"""Five turns timed out and every record named a clock that had not expired.

MEASURED on run `r6aa597a8_a0` (2026-09-13 03:19→04:07), read out of its own transcript:

    turn 2  timeout=241.3  origin=socket_turn  budget_s=240  treatment=retry  transient=1
    turn 3  timeout=240.5  origin=socket_turn  budget_s=240  treatment=retry  transient=2
    turn 4  timeout=240.9  turn 5  timeout=240.6  turn 6  timeout=240.6

`SOCKET_TURN_TIMEOUT_S` is 1200. Every one of those rows says a 1200-second clock expired at
240, which is precisely the reading the `origin` field exists to prevent -- its own docstring:
*"Without it an inner overrun reads as an outer one."*

THE DEFECT IS THE LABEL, NOT THE BUDGET. `_origin` was chosen from `self.socket` -- the worker's
TRANSPORT -- while the comparison was always against `per_turn_timeout_s`, and `_note_timeout`
hard-coded `budget_s=self.per_turn_timeout_s` regardless. So the label and the number could not
disagree with each other even when the pair was wrong.

WHY THE BUDGET IS LEFT ALONE, WHICH IS THE SECOND VERSION OF THIS FILE. Raising it to
SOCKET_TURN_TIMEOUT_S looked right -- `_defer_generation` says in as many words that a socket
turn is bounded by its own clock and that the tab-era budget is deliberately skipped for it.
Then the per-turn timings of the same run were read:

    turn 1  sent 03:20:25  reply +90s   no timeout
    turn 2  sent 03:21:58  NO REPLY     timeout +241s
    turn 3  sent 03:33:04  reply +70s   timeout +240s   <-- both
    turn 4  sent 03:37:06  NO REPLY     timeout +241s
    turn 5  sent 03:42:28  NO REPLY     timeout +241s
    turn 6  sent 03:51:50  NO REPLY     timeout +241s
    turn 7  sent 03:59:41  reply +22s   no timeout
    turn 8  sent 04:01:18  reply +100s  no timeout
    turn 9  sent 04:04:22  reply +164s  no timeout

**Every reply that arrived came within 22-164 seconds**, comfortably inside 240. And turn 3
replied at +70s and was declared timed out at +240s anyway. A longer budget would not have saved
turn 3 -- it would have made it wait 1200 seconds for an answer it already had. The change would
have made the one measured instance of this failure worse, so it was taken back.

WHAT IS WRONG WITH TURN 3 IS NOT DETERMINED. A reply was recorded at +70s and the poll was still
in `waiting` at +240s. Why the poll did not see a reply it had is not answerable from the
transcript, and widening a clock to cover for it would hide it.
"""
from __future__ import annotations

import io
import json
import os
import sys

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

def test_the_row_carries_the_budget_that_was_compared():
    w = _worker(socket=True)
    w._note_timeout("per_turn", 241.3, "retry", budget_s=w.per_turn_timeout_s)
    row = w._tx.rows[0]
    assert row["origin"] == "per_turn"
    assert row["budget_s"] == w.per_turn_timeout_s


def test_an_omitted_budget_falls_back_to_the_old_field():
    """Other callers may exist; none should learn a new required argument from this fix."""
    w = _worker(socket=True)
    w._note_timeout("per_turn", 5.0, "retry")
    assert w._tx.rows[0]["budget_s"] == w.per_turn_timeout_s


def test_recording_a_timeout_cannot_raise():
    """It runs beside a failure path, and a failure path that can fail again is worse than no
    record. Its own docstring says so."""
    w = _worker(socket=True)
    w._tx = None
    w._note_timeout("per_turn", 1.0, "retry", budget_s=240)
    assert True  # reaching here is the assertion


# ── the branch says which clock it used ───────────────────────────────────────────────────

def _source_of(fn_name):
    src = io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("    def %s(" % fn_name)
    return src[i:src.index("\n    def ", i + 10)]


def test_the_origin_is_not_taken_from_the_transport():
    """THE DEFECT. A socket worker timing out on the 240s budget recorded `socket_turn`, so the
    field could never report what it was added to report."""
    body = _source_of("poll")
    assert '_origin = "per_turn"' in body, "origin を実際に比較した時計から取っていない"
    assert 'socket_turn" if getattr(self, "socket"' not in body, (
        "origin をワーカーの transport から選ぶ書き方が戻っている")


def test_the_budget_is_still_the_per_turn_one():
    """PINNED SO THE REVERT STAYS REVERTED. Raising this is a decision about how long to wait,
    and the timings above say it would not have helped the run that prompted it."""
    body = _source_of("poll")
    assert "_bound = self.per_turn_timeout_s" in body
    assert "SOCKET_TURN_TIMEOUT_S if getattr(self, \"socket\", False)" not in body


def test_the_branch_still_reports_what_it_did():
    body = _source_of("poll")
    for treatment in ('"retry"', '"salvaged"', '"stuck"'):
        assert "_note_timeout(_origin, _elapsed, %s" % treatment in body, treatment


# ── against the run it was found in ───────────────────────────────────────────────────────

TRANSCRIPT = os.path.join(REPO, ".fleet", "transcripts", "r6aa597a8_a0_w0.jsonl")


def test_every_reply_that_arrived_was_well_inside_the_budget():
    """The measurement that took the bound change back. If this ever stops holding, raising the
    budget becomes arguable again -- on evidence, not on a docstring."""
    if not os.path.isfile(TRANSCRIPT):
        pytest.skip("the transcript for r6aa597a8_a0 is no longer on this machine")
    rows = [json.loads(l) for l in io.open(TRANSCRIPT, encoding="utf-8", errors="replace")
            if l.strip()]
    sent = {r["turn"]: r["ts"] for r in rows if r.get("role") == "user" and r.get("ts")}
    replied = {r["turn"]: r["ts"] for r in rows if r.get("role") == "assistant" and r.get("ts")}
    gaps = [replied[t] - sent[t] for t in replied if t in sent]
    assert gaps, "前提が崩れている: 応答のあるターンが無い"
    assert max(gaps) < 240, [round(g) for g in gaps]


def test_the_turn_that_both_replied_and_timed_out():
    """The case a longer budget would have made worse, pinned by name."""
    if not os.path.isfile(TRANSCRIPT):
        pytest.skip("the transcript for r6aa597a8_a0 is no longer on this machine")
    rows = [json.loads(l) for l in io.open(TRANSCRIPT, encoding="utf-8", errors="replace")
            if l.strip()]
    a3 = [r for r in rows if r.get("role") == "assistant" and r.get("turn") == 3][0]
    m3 = [r for r in rows if r.get("name") == "timeout" and r.get("turn") == 3][0]
    assert a3["ts"] < m3["ts"]
    assert m3["ts"] - a3["ts"] > 120, round(m3["ts"] - a3["ts"])
    # A 1200s bound would have widened exactly this gap by another sixteen minutes.
    assert RF.SOCKET_TURN_TIMEOUT_S > 4 * m3["budget_s"]
