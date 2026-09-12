# -*- coding: utf-8 -*-
"""A human intervened and the repetition counter carried on counting across them.

TWO COUNTERS, ONE RESET. `_continue_count` (replies that report progress and never finish) is
reset the moment a steer is consumed -- three separate places say so, each with the comment
"a steer is real progress". `no_progress` (the VERBATIM-identical-reply counter, which is the
one that terminates a worker as stuck) is reset nowhere on that path, and neither is
`last_norm`, the key it compares against.

So a worker sitting at `no_progress = 2` with `max_no_progress = 3` can be steered by a person
and still be declared STUCK on the very next reply, because that reply is compared against a
key from BEFORE the intervention. The person's message is in the transcript; the worker is
recorded as having made no progress for three turns.

WHERE THE IDEA CAME FROM, and it is worth naming since almost nothing else did. DeepSeek's
`repeat-tool-reminder` counts consecutive identical tool calls and resets the chain on a user
interruption -- "repetition after a human intervenes is not a loop". The argument-normalisation
half of that plugin does not transfer here at all (this fleet cannot see the agent's tool calls,
only its reply text), but that one semantic does, because this fleet has exactly the same shape
in `_norm_for_progress`.

LIVE INCIDENCE IS NOT MEASURED. The retained transcripts were not enough to show a real worker
terminated this way -- **cause of any specific past STUCK not identified**, and no claim is made
that one happened. What is established is the mechanism, by construction, below. That is why
this file is written as a characterization first: `test_the_chain_used_to_cross_a_steer` states
the old behaviour and what makes it impossible now.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.relay_fleet as rf                      # noqa: E402
from relay.relay_fleet import RelayWorker           # noqa: E402


def _worker(max_no_progress=3):
    w = RelayWorker({"text": "長い調べもの"}, "w0", fanout=False,
                    max_no_progress=max_no_progress)
    w.status = "waiting"
    return w


SAME = "まだ調べています。もう少しかかります。"


def _reply(w, text):
    """One turn: whatever _decide does with a reply, including the progress bookkeeping."""
    w._decide(text)


# ── the mechanism ─────────────────────────────────────────────────────────────────────────

def test_a_repeated_reply_still_counts_toward_stuck():
    """The counter must keep working. This is what a steer reset must not break."""
    w = _worker()
    for _ in range(3):
        _reply(w, SAME)
    assert w.no_progress >= 2, "繰り返し検出そのものが壊れている: %r" % w.no_progress


def test_consuming_a_steer_clears_the_repetition_chain():
    """THE FIX. A person intervened; what came before is not evidence about what comes after."""
    w = _worker()
    _reply(w, SAME)
    _reply(w, SAME)
    before = w.no_progress
    assert before >= 1, "前提が崩れている（連続カウントが上がっていない）: %r" % before

    w.steer_msgs.append("そこはもういい。2月分だけ先に出して。")
    w._begin_send()                       # the turn that actually delivers the steer

    assert w.no_progress == 0, (
        "人が割り込んだのに繰り返しカウントが続いている（%d）-- 次の1返信で STUCK になりうる"
        % w.no_progress)
    assert w.last_norm is None, (
        "比較キーが割り込み前のまま。カウントを0にしても、次の返信がそのキーと一致すれば"
        "すぐ1に戻るので、片方だけでは足りない")


def test_the_chain_after_a_steer_still_trips_at_the_threshold():
    """Cleared, not disabled. A worker that genuinely loops after being steered must still
    stop -- otherwise this turns a stuck detector into a way of never stopping."""
    w = _worker(max_no_progress=3)
    w.steer_msgs.append("別の角度で。")
    w._begin_send()
    for _ in range(4):
        _reply(w, SAME)
    assert w.no_progress >= 2, (
        "steer 後に繰り返し検出が効かなくなっている: %r" % w.no_progress)


def test_the_continue_counter_was_already_reset_and_still_is():
    """The asymmetry that made this visible: one of the two counters always had this rule."""
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    assert src.count("self._continue_count = 0") >= 3


def test_the_chain_used_to_cross_a_steer():
    """CHARACTERIZATION. The old code marked `_last_was_steer` and reset nothing else, so the
    reset had to be absent from the steer branch for the defect to exist. Pinned as a source
    fact because the behaviour it describes can no longer be produced at runtime.
    """
    src = open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()
    i = src.index("if self.steer_msgs:")
    branch = src[i:src.index("else:", i)]
    assert "self._last_was_steer = True" in branch
    assert "self.no_progress = 0" in branch, (
        "steer を配ったターンで繰り返しカウントが戻されていない")
    assert "self.last_norm = None" in branch


def test_a_turn_with_no_steer_does_not_clear_the_chain():
    """The reset belongs to the steer, not to every send. Clearing it on an ordinary turn
    would mean the counter never reaches its threshold and the detector never fires."""
    w = _worker()
    _reply(w, SAME)
    _reply(w, SAME)
    before, key = w.no_progress, w.last_norm
    w._begin_send()
    assert w.no_progress == before and w.last_norm == key
