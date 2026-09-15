# -*- coding: utf-8 -*-
"""A worker that already answered the question does not need to be asked again.

WHAT HAPPENED (mined transcript, .fleet/transcripts/r6aa8fc73_a0_w0.jsonl -- read-only, not
checked in; described here without any of its business content). A worker was asked to find one
characteristic of a raw material, exhaustively searched the one data source it had been pointed
at, and on turn 2 reported STUCK with a precise, correct diagnosis: the name it was given did not
match anything in that source, and it needed to know which name to look for instead. That was the
right answer to give.

The relay's fleet path then sent it the SAME byte-identical "this was probably a transient
network hiccup, try again" nudge NINE more times, once per turn, because each of the worker's
STUCK replies used different wording to say the same thing and so never matched the exact-repeat
guard that exists to catch a truly dead endpoint. Three defects, all in this one branch of
relay_fleet.py's RelayWorker._decide:

  1. The nudge text itself never escalated. copilot_autopilot_relay.py already has
     _next_retry_job(count) for exactly this (counts 1-2 unchanged for back-compat, count 3+
     rotates through stronger phrasing) -- this call site just wasn't using it.
  2. Every escalation phrase assumed a MECHANICAL failure (bad argument, wrong path, missing
     permission). None of them said the thing that would have helped: the place already searched
     might not be the right place.
  3. Nothing noticed that the worker kept reaching the SAME conclusion. The existing no-progress
     guard (NET_RETRY_NOPROGRESS_MAX) only catches a reply that repeats near byte-for-byte; a
     worker that restates the same finding in fresh words every turn sails straight past it.

A SECOND DEFECT IN THE SAME INCIDENT. Several of those replies didn't just restate the finding --
they asserted, in so many words, that every possible route had already been checked. The
assertion was false: one table that would have held the answer was never opened. A worker that
believes it has already looked everywhere cannot be talked out of that belief by a nudge that
argues with it ("consider a different source" gets back a restatement of the same claim -- which
is exactly what those turns were). A second, unrelated run the same day showed the identical
shape from a different cause: a worker whose tools were locked wrote that it had verified content
against a source it could not actually read. The fix is not to argue with the claim but to ask
for something checkable: an enumeration of what was actually looked at.

WHAT THIS FILE PROVES, all without a browser (RelayWorker._decide is driven directly with
scripted text, the same pattern as test_continue_escalation.py):

  * _next_retry_job keeps counts 1-2 byte-identical to RETRY_JOB (back-compat for the common
    fast case) and count 3 differs.
  * The escalation set includes a phrase about trying a different source, and -- like every
    nudge in this file -- none of them name a concrete one.
  * Two consecutive STUCK replies that reach substantially the same conclusion (worded
    differently) stop the retrying instead of sending another nudge, and the worker's OWN
    stated reason reaches the outcome.
  * Two consecutive STUCK replies that say different things do NOT stop it -- the worker is
    still making progress on the problem and has earned its retries.
  * A reply that claims exhaustive coverage gets asked to enumerate what it actually checked,
    not argued with -- and that ask still counts as a turn toward convergence, not a fresh
    retry budget: repeating the same exhaustive claim after being asked to enumerate still
    stops the run.

Run:  .venv\\Scripts\\python.exe -m pytest -q relay/test_a_nudge_that_repeats_itself_is_not_a_retry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from relay.copilot_autopilot_relay import RETRY_JOB, _RETRY_ESCALATION_PHRASES, _next_retry_job
from relay.relay_fleet import (
    RelayWorker,
    TERMINAL,
    _claims_exhaustive_search,
    _EXHAUSTIVE_CLAIM_NUDGE,
    _stuck_converged,
    _stuck_retry_nudge,
    stuck_reason_text,
)

#: A word no nudge in this file may contain -- naming a concrete source in the text sent to the
#: worker is exactly the cheat this repository forbids elsewhere (the answer must never be
#: written into the prompt); it would also just be a guess, since nothing here can see what the
#: worker's real data actually holds.
_BANNED_SOURCE_WORDS = ("excel", "エクセル", "データベース", "database", "csv", "sql")

#: Two STUCK reasons that reach the SAME finding ("checked the list, nothing matched") in
#: different words -- close enough that a person reading both would call them one answer
#: restated, which is the case _stuck_converged exists to catch.
_SAME_FINDING_A = ("対象データを一覧で確認しましたが対象データに該当する記録は"
                    "見つかりませんでした。関連する一覧も確認済みです")
_SAME_FINDING_B = ("一覧を確認した結果、対象データに該当する記録はやはり"
                    "見つかりませんでした。関連する一覧は既に確認済みです")

#: A STUCK reason on a completely different topic -- a worker reporting THIS after _SAME_FINDING_A
#: is not repeating itself, it has moved to a different problem, and must not be stopped.
_DIFFERENT_FINDING = ("接続に必要な認証情報を取得しようとしましたが認証エラーが返され、"
                       "処理をこれ以上継続することができない状態です。"
                       "設定の見直しが必要そうです")

#: Two replies that both assert exhaustive coverage (matching _EXHAUSTIVE_CLAIM_MARKERS) of the
#: same search, worded differently -- the pair the enumeration-ask/convergence interaction test
#: needs: the SECOND one must still be recognised as a repeat of the same (false) claim.
_EXHAUSTIVE_CLAIM_A = ("関連しそうな一覧をすべて確認しましたが、指定された名称に該当する記録は"
                       "どの一覧にも見つかりませんでした。関連する経路はすべて確認済みで、"
                       "追加の探索経路はありません")
_EXHAUSTIVE_CLAIM_B = ("関連しそうな一覧をあらためてすべて確認しましたが、指定された名称に"
                       "該当する記録はやはりどの一覧にも見つかりませんでした。関連する経路は"
                       "すべて確認済みで、追加の探索経路はありません")

#: What answering the enumeration ask HONESTLY looks like: a worker that actually checks its own
#: turns and finds something it had not, in fact, looked at. Real progress, and the whole reason
#: the enumeration ask exists rather than a nudge that argues with the claim -- this must NOT
#: read as a repeat of _EXHAUSTIVE_CLAIM_A just because it also starts from the same dead end.
_NEW_FINDING_AFTER_ENUMERATION = (
    "改めて確認範囲を洗い出したところ、一覧Dだけはまだ一度も確認していませんでした。"
    "次はそちらを個別に確認する必要があります")


def test_first_two_retries_are_byte_identical_the_third_is_not():
    assert _next_retry_job(1) == RETRY_JOB
    assert _next_retry_job(2) == RETRY_JOB
    assert _next_retry_job(3) != RETRY_JOB
    # never byte-identical to its immediate predecessor from the third retry on
    seen = {_next_retry_job(c) for c in range(3, 8)}
    assert len(seen) > 1


def test_the_escalation_set_offers_a_different_source_and_names_none():
    # at least one phrase steers the worker toward a DIFFERENT source/route rather than
    # re-checking the same call's arguments/paths/permissions
    assert any(k in phrase for phrase in _RETRY_ESCALATION_PHRASES
               for k in ("他に", "別の", "異なる場所", "違う"))
    for phrase in _RETRY_ESCALATION_PHRASES:
        for word in _BANNED_SOURCE_WORDS:
            assert word not in phrase.lower(), "escalation phrase names a concrete source"
    for word in _BANNED_SOURCE_WORDS:
        assert word not in _EXHAUSTIVE_CLAIM_NUDGE.lower(), \
            "enumeration nudge names a concrete source"


def test_a_lone_stuck_reply_always_earns_its_one_retry():
    """Turn 2 of the mined transcript -- the FIRST STUCK of a streak -- must never be treated
    as a repeat: there is nothing before it to have converged with."""
    w = RelayWorker("some goal", "w_first", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    assert w.status == "ready"
    assert w.status not in TERMINAL


def test_two_consecutive_similar_stuck_replies_stop_the_retrying():
    w = RelayWorker("some goal", "w_converge", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    assert w.status == "ready", "the first STUCK must still get its retry"
    w._decide("STUCK: " + _SAME_FINDING_B)
    assert w.status in TERMINAL
    assert w.outcome == "STUCK"
    # the worker's OWN reason reached the outcome -- not a generic "gave up"
    assert "見つかりません" in w.reason
    assert "gave up" not in (w.reason or "").lower()


def test_two_consecutive_different_stuck_replies_do_not_stop_it():
    """A worker whose second STUCK is about something else entirely is still making progress
    on the problem (it ruled one thing out and hit a new obstacle) and must keep its retries."""
    w = RelayWorker("some goal", "w_diverge", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    assert w.status == "ready"
    w._decide("STUCK: " + _DIFFERENT_FINDING)
    assert w.status == "ready", "different STUCK reasons must not be treated as convergence"
    assert w.status not in TERMINAL


def test_stuck_converged_is_pure_and_conservative_below_the_word_floor():
    assert _stuck_converged(_SAME_FINDING_A, _SAME_FINDING_B) is True
    assert _stuck_converged(_SAME_FINDING_A, _DIFFERENT_FINDING) is False
    # too little content on either side is not evidence either way -- never stop on it
    assert _stuck_converged("不明", "不明") is False
    assert _stuck_converged("", _SAME_FINDING_A) is False


def test_stuck_reason_text_extracts_the_workers_own_words():
    resp = "分析結果を以下にまとめます。長い説明。 STUCK: 結局これが理由です"
    assert stuck_reason_text(resp) == "結局これが理由です"
    # the LAST marker wins when the word is discussed earlier in the reasoning too
    resp2 = "STUCKという語には触れません。 STUCK: 本当の理由はこちら"
    assert stuck_reason_text(resp2) == "本当の理由はこちら"


def test_an_exhaustive_coverage_claim_is_asked_to_enumerate_not_argued_with():
    assert _claims_exhaustive_search("STUCK: " + _EXHAUSTIVE_CLAIM_A) is True
    assert _claims_exhaustive_search("STUCK: " + _DIFFERENT_FINDING) is False
    # the override applies even at retry count 1-2, the back-compat window that would
    # otherwise keep sending the unchanged RETRY_JOB constant
    nudge = _stuck_retry_nudge("STUCK: " + _EXHAUSTIVE_CLAIM_A, 1)
    assert nudge == _EXHAUSTIVE_CLAIM_NUDGE
    assert nudge != RETRY_JOB
    # a plain (non-exhaustive-claim) STUCK is unaffected -- normal escalation still applies
    assert _stuck_retry_nudge("STUCK: " + _DIFFERENT_FINDING, 1) == RETRY_JOB
    assert _stuck_retry_nudge("STUCK: " + _DIFFERENT_FINDING, 3) == _next_retry_job(3)


def test_the_enumeration_ask_counts_toward_convergence_not_a_fresh_budget():
    """The operator's own correction: an enumeration ask must be ONE chance to notice a gap,
    not a new retry budget. A worker that repeats the same exhaustive claim after being asked
    to enumerate must still be stopped on that very next reply."""
    w = RelayWorker("some goal", "w_exhaustive", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    assert w.status == "ready"
    w._decide("STUCK: " + _EXHAUSTIVE_CLAIM_A)
    assert w.status == "ready", "first exhaustive claim differs from turn 1 -> not converged yet"
    assert _EXHAUSTIVE_CLAIM_NUDGE in w.job, "should be asked to enumerate, not re-nudged blindly"
    # the worker answers the enumeration ask by repeating the same (unfalsifiable) claim
    w._decide("STUCK: " + _EXHAUSTIVE_CLAIM_B)
    assert w.status in TERMINAL, "repeating the claim after being asked to enumerate must stop"
    assert w.outcome == "STUCK"
    assert "確認済み" in w.reason or "見つかりません" in w.reason


def test_an_enumeration_reply_that_still_concludes_the_same_thing_converges():
    """An enumeration reply is EXPECTED to look nothing like the conclusion it follows -- it is
    a list of what was actually queried, not a restated finding -- so a naive whole-reply
    comparison would read it as "not converged" and hand back an undeserved extra retry. That
    is why stuck_reason_text only ever compares the text AFTER the reply's own final
    "STUCK: <reason>" marker: whatever list precedes it, a worker that still lands on the same
    underlying claim must still be caught by _stuck_converged, because the compared text is
    just the claim again."""
    w = RelayWorker("some goal", "w_enum_same", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    w._decide("STUCK: " + _EXHAUSTIVE_CLAIM_A)
    assert _EXHAUSTIVE_CLAIM_NUDGE in w.job
    enumeration_reply = (
        "ご指示の通り確認済みの対象を列挙します。1) 一覧A 2) 一覧B 3) 一覧C を"
        "それぞれ個別に確認しました。STUCK: " + _EXHAUSTIVE_CLAIM_B
    )
    w._decide(enumeration_reply)
    assert w.status in TERMINAL, "the list is new text, but the closing claim repeats"
    assert w.outcome == "STUCK"


def test_an_enumeration_reply_with_a_new_finding_does_not_converge():
    """The other half of the same decision: when the enumeration ask does its job and the
    worker surfaces something it had genuinely not checked, that is real progress -- the exact
    outcome this whole mechanism exists to enable -- and must NOT be mistaken for a repeated
    conclusion just because the reply also still ends in STUCK."""
    w = RelayWorker("some goal", "w_new_finding", max_transient=10, max_no_progress=100)
    w._decide("STUCK: " + _SAME_FINDING_A)
    w._decide("STUCK: " + _EXHAUSTIVE_CLAIM_A)
    assert _EXHAUSTIVE_CLAIM_NUDGE in w.job
    w._decide("STUCK: " + _NEW_FINDING_AFTER_ENUMERATION)
    assert w.status == "ready", "a genuinely new finding must not be treated as convergence"
    assert w.status not in TERMINAL


if __name__ == "__main__":
    raise SystemExit(
        __import__("pytest").main([__file__, "-q"])
    )
