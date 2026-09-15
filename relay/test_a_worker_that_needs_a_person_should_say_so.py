# -*- coding: utf-8 -*-
"""A worker that cannot proceed without a person should say so to a person, not to a log line.

THE INCIDENT (mined transcript, .fleet/transcripts/r6aa8fc73_a0_w0.jsonl -- read-only, not
checked in; described here without any of its business content -- see relay/relay_fleet.py's
GATE_AFTER_STUCK_RETRIES comment for the paraphrase). A worker was asked to look up a value in
a database under an item name it had been given. On turn 2 it reported STUCK with a precise,
answerable question: that exact name did not appear anywhere in the tables it had searched, and
it needed to know which actual term to search for instead. Nobody was ever asked. Turns 3
through 11 spent nine nudges -- at the time, byte-identical ones, telling the worker its earlier
failure was probably a transient blip and to just try again -- on a question that was never going
to resolve without a person supplying the missing term. The operator's own diagnosis, verbatim:
"絶対に人間の助けがいる場合はその時点で通知を出して追加の指示を求めないといけないのでは...
10回のretryを待つだけ待つんじゃなく通知を出して人間の入力を待つとかを5回目あたりに出せば
いいんじゃないの" -- if a person is definitely needed, say so and wait for them, around the
fifth retry, rather than exhausting all ten.

WHAT WAS ALREADY THERE, AND WHAT WAS MISSING. tools/gate_ops.py could already pause a loop and
ask a human (gate_ask/gate_poll/gate_answer), and fleet_runner.py's _pending_gates() already
surfaced any open gate into status.json's pending_gates for FleetCockpit's existing approval
banner to show and answer. A same-day fix (relay/test_a_nudge_that_repeats_itself_is_not_a_retry
.py) already taught _decide to notice when two consecutive STUCK replies reach the same
conclusion reworded (_stuck_converged) -- but that fix's own answer to noticing was still to
settle STUCK immediately, not to ask. Nothing in the whole stack ever CALLED gate_ask from the
one place that actually knows a worker is stuck: the relay's own _decide.

THIS FILE PROVES, all without a browser (RelayWorker._decide driven directly with scripted
text, the same pattern as test_a_nudge_that_repeats_itself_is_not_a_retry.py):

  * A worker whose STUCK reason converges with its own immediately preceding one raises a gate
    carrying ITS OWN stated reason as the question -- not a template -- and holds
    (status='awaiting_gate', not terminal) instead of settling STUCK on the spot.
  * A worker past MAX_UNLOCK_ATTEMPTS raises a gate naming what it actually needs (the same
    actionable reason _inject_unlock always wrote to a log line nobody read), instead of
    going straight to STUCK.
  * A worker whose STUCK wording keeps drifting just enough to dodge convergence still stops
    asking for retries once GATE_AFTER_STUCK_RETRIES is reached, and asks a human instead --
    the "around the fifth retry" the operator asked for, named and justified where it is
    defined in relay/relay_fleet.py.
  * An answered gate resumes the SAME conversation as the worker's next turn (no new
    RelayWorker, no fresh_replay, no lost transcript) rather than restarting the goal.
  * An unanswered gate does not hold a worker forever: past GATE_ANSWER_TIMEOUT_S it settles
    STUCK with the question itself preserved in the reason.
  * A worker that has already asked does not ask again while its question stands -- one gate
    per worker at a time.

Run:  .venv\\Scripts\\python.exe -m pytest -q relay/test_a_worker_that_needs_a_person_should_say_so.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import relay.relay_fleet as rf
from relay.relay_fleet import (
    RelayWorker,
    TERMINAL,
    GATE_AFTER_STUCK_RETRIES,
    GATE_ANSWER_TIMEOUT_S,
    MAX_UNLOCK_ATTEMPTS,
)
from tools.gate_ops import gate_answer, gate_get

#: Two STUCK reasons that reach the SAME finding in different words -- close enough that
#: _stuck_converged (calibrated separately, see its own test file) reads them as one
#: conclusion restated. Mirrors the shape of the mined incident's turns 5-6, not its content.
_SAME_FINDING_A = ("対象の項目名を一覧で確認しましたが、指定された名称に一致する項目は"
                    "見つかりませんでした。関連する一覧も確認済みです")
_SAME_FINDING_B = ("一覧をあらためて確認した結果、対象の項目名はやはり指定された名称に"
                    "一致する項目が見つかりませんでした。関連する一覧は既に確認済みです")

#: Five STUCK reasons on five unrelated topics -- deliberately sharing almost no words, so
#: _stuck_converged never fires on any consecutive pair (each is a fresh non-convergent STUCK,
#: the shape that dodges convergence and must instead be caught by the retry-count backstop).
_UNRELATED_REASONS = [
    "STUCK: the customer address field could not be parsed from the uploaded spreadsheet",
    "STUCK: the printer queue on the shared file server appears to be offline this morning",
    "STUCK: the calendar invite template is missing a required timezone parameter entirely",
    "STUCK: the vendor contract number does not match any record kept in the archive folder",
    "STUCK: the shipping label generator returned an unexpected and unrecognised currency code",
]


def _fresh_worker(name):
    return RelayWorker("some goal", name, max_transient=10, max_no_progress=100)


def test_a_converged_worker_raises_a_gate_carrying_its_own_question():
    w = _fresh_worker("w_converge_gate")
    w._decide("STUCK: " + _SAME_FINDING_A)
    assert w.status == "ready", "the first STUCK of a streak always earns its one retry"
    w._decide("STUCK: " + _SAME_FINDING_B)
    # ASKS, does not settle -- holding, not terminal.
    assert w.status == "awaiting_gate"
    assert w.status not in TERMINAL
    assert w._gate_token, "a gate must actually have been raised"
    gate = gate_get(w._gate_token)
    assert gate is not None
    # THE WORKER'S OWN WORDS, NOT A TEMPLATE. The question is the worker's stated reason, not
    # a generic "a worker needs help" placeholder that tells the operator nothing.
    assert "見つかりません" in gate["question"]
    assert gate["question"] != "" and "a worker needs help" not in gate["question"].lower()


def test_a_worker_past_the_unlock_attempts_raises_a_gate_naming_what_it_needs():
    orig_pw = rf._unlock_password
    rf._unlock_password = lambda: "unit-test-password"
    try:
        w = _fresh_worker("w_unlock_gate")
        locked = ("[locked client IP: '203.0.113.9'] Call unlock(password='<password>') "
                  "first. The unlock is stored per client IP for 30 days.")
        # The constructor already spends one attempt proactively (a local password exists),
        # so MAX_UNLOCK_ATTEMPTS - 1 more reactive injections reach the cap exactly.
        for _ in range(MAX_UNLOCK_ATTEMPTS - 1):
            w._decide(locked)
        assert w._unlock_attempts == MAX_UNLOCK_ATTEMPTS
        w._decide(locked)                      # one past the cap
        assert w.status == "awaiting_gate", "unlock exhaustion must ask a human, not settle STUCK"
        assert w.status not in TERMINAL
        assert w._gate_token
        gate = gate_get(w._gate_token)
        assert gate is not None
        # NAMES WHAT IT NEEDS -- the actionable causes _inject_unlock always knew, now reaching
        # a person instead of only a log line: the token-enforcement possibility by name, and
        # never the password itself.
        assert "unlock_token" in gate["question"]
        assert "unit-test-password" not in gate["question"]
    finally:
        rf._unlock_password = orig_pw


def test_the_retry_budget_backstop_asks_before_the_full_budget_is_spent():
    """A STUCK streak whose wording keeps changing enough to dodge convergence on every single
    pair must still stop short of spending the whole max_transient budget re-asking a question
    only a person can answer -- GATE_AFTER_STUCK_RETRIES is the backstop for exactly this shape,
    which is the shape the mined incident's turns 2-5 actually had (no consecutive pair among
    them clears STUCK_CONVERGENCE_SIMILARITY; convergence only caught turns 5-6)."""
    w = _fresh_worker("w_retry_budget_gate")
    for reason in _UNRELATED_REASONS:
        if w.status == "awaiting_gate":
            break
        w._decide(reason)
    assert w.status == "awaiting_gate", (
        "must have asked a human by the %dth un-converged STUCK reply" % len(_UNRELATED_REASONS))
    assert w.status not in TERMINAL
    # Reached WELL BEFORE the full max_transient=10 budget -- the whole point of the backstop.
    assert w.transient < 10
    assert w._gate_token
    gate = gate_get(w._gate_token)
    assert gate is not None and gate["question"]


def test_an_answered_gate_resumes_the_same_conversation_not_a_restart():
    w = _fresh_worker("w_resume_gate")
    w._decide("STUCK: " + _SAME_FINDING_A)
    w._decide("STUCK: " + _SAME_FINDING_B)
    assert w.status == "awaiting_gate"
    token = w._gate_token
    tx_before = w._tx                       # SAME transcript object == same conversation
    replays_before = w.fresh_replay_count   # 0, and must STAY 0 -- no fresh conversation opened
    gate_answer(token, "探すべき項目名は「テスト項目A」です")
    terminal = w.poll()                     # dispatches to _poll_gate() while awaiting_gate
    assert terminal is False, "an answer resumes -- it is not itself a terminal outcome"
    assert w.status == "ready", "resuming means the answer becomes the NEXT TURN, sent as usual"
    assert w._gate_token is None, "the standing gate is cleared once answered"
    # THE SAME CONVERSATION, NOT A NEW ONE.
    assert w._tx is tx_before
    assert w.fresh_replay_count == replays_before
    assert w.goal == "some goal"            # the original goal text, untouched
    # THE ANSWER ACTUALLY REACHES THE WORKER, carrying the question it answers along with it,
    # not silently dropped or reduced to a bare "continue".
    assert "テスト項目A" in w.job
    assert "見つかりません" in w.job         # the original question is still present for context


def test_an_unanswered_gate_settles_stuck_with_the_question_preserved():
    w = _fresh_worker("w_timeout_gate")
    w._decide("STUCK: " + _SAME_FINDING_A)
    w._decide("STUCK: " + _SAME_FINDING_B)
    assert w.status == "awaiting_gate"
    question = w._gate_question
    assert question
    # Force the standing gate's deadline into the past instead of sleeping GATE_ANSWER_TIMEOUT_S
    # (1800s by default) -- this is the SAME clock _poll_gate reads, just wound forward.
    w._gate_deadline = time.time() - 1.0
    terminal = w.poll()
    assert terminal is True
    assert w.status == "stuck"
    assert w.outcome == "STUCK"
    assert w._gate_token is None
    # THE RECORD SAYS WHAT WAS ASKED AND THAT NOBODY ANSWERED -- not a generic timeout message
    # that has already lost the question by the time anyone reads it.
    assert question in w.reason
    assert str(int(GATE_ANSWER_TIMEOUT_S)) in w.reason


def test_a_worker_with_a_standing_gate_does_not_raise_a_second():
    w = _fresh_worker("w_one_gate_gate")
    w._decide("STUCK: " + _SAME_FINDING_A)
    w._decide("STUCK: " + _SAME_FINDING_B)
    assert w.status == "awaiting_gate"
    first_token = w._gate_token
    assert first_token
    # Whatever next decided a person is needed (convergence again, unlock exhaustion, the
    # retry backstop) must not open a second question while the first still stands.
    raised_again = w._raise_stuck_gate("a completely different question", "manual-test-trigger")
    assert raised_again is False
    assert w._gate_token == first_token, "the ORIGINAL gate must still be the standing one"
    gate = gate_get(first_token)
    assert gate is not None
    assert "a completely different question" not in gate["question"]


if __name__ == "__main__":
    raise SystemExit(
        __import__("pytest").main([__file__, "-q"])
    )
