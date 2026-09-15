# -*- coding: utf-8 -*-
"""A lock refusal too long to trust by length alone must be asked about, not guessed at.

THE INCIDENT. _looks_locked()'s dominance rule (LOCKED_DOMINANCE_MAX_CHARS, 400 chars) exists
for a real reason -- see the 2026-07-13 comment above LOCKED_MARKERS: a security-review worker's
long prose about the unlock() API false-tripped the old loose markers and caused four spurious
auto-unlocks. The fix required BOTH a distinctive marker AND a short reply, and that second half
is unconditional: a genuine lock refusal that happens to arrive wrapped in a long reply is
treated exactly like the false positive it was designed to reject.

That is what actually happened to r6aa8e10b_a0_w0 on 2026-09-15. Its IP was unlocked but no
per-call unlock_token was attached, the server refused it a third time, and the worker wrote a
long multi-paragraph analysis that QUOTED "[locked: no valid unlock token]" (the shortened form
the server actually emits when the IP portion is elided) and concluded the task was blocked on
authentication. len(resp) >= LOCKED_DOMINANCE_MAX_CHARS, so neither of _looks_locked()'s two
branches fired, no unlock was ever injected, and the goal was lost -- the worker guessed a
passphrase from memory and failed.

THE FIX UNDER TEST. relay_fleet._looks_locked_ambiguous(resp) names exactly the case
_looks_locked drops on purpose: a LOCKED_MARKERS marker present, but the reply is at or past the
dominance ceiling. RelayWorker._decide, on hitting it, does not guess -- it sends the worker
relay_fleet.LOCK_PROBE_QUESTION ("was the immediately preceding call_tool refused as locked?
answer only yes or no") as an ordinary turn and waits for the worker's own account of what it
saw, before deciding. This is additive: _looks_locked's own two branches are untouched, and
_looks_locked_ambiguous can only be True where _looks_locked is already False (the two share the
marker check but require opposite dominance outcomes), so nothing this file exercises can change
anything _looks_locked already decided.

Bounded to ONE probe per worker, EVER: `_lock_probe_used` is a lifetime flag, not a per-episode
one, and a fresh ambiguous reply arriving after the budget is already spent falls straight
through to today's behaviour (not locked) instead of asking again.

Run:  .venv\\Scripts\\python.exe -m pytest relay\\test_when_a_marker_is_too_long_to_trust_ask_the_worker.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import relay_fleet as RF  # noqa: E402
from relay.relay_fleet import RelayWorker  # noqa: E402

PW = "lock_probe_test_pw_7q"

# The exact shape of the lost incident: a long, first-person account of a tool call that quotes
# the server's shortened token-missing marker and concludes the task is stuck on it. Ambiguous
# on purpose -- this is prose ABOUT a tool result, which is indistinguishable by length/marker
# alone from prose ABOUT the unlock() API in general (the 2026-07 false positive). Only the
# worker's own follow-up answer can tell the two apart.
LONG_AMBIGUOUS_REPLY = (
    "指定されたツールを call_tool で実行したところ、応答本文には次の文字列が含まれていました。"
    "そのまま引用します: [locked: no valid unlock token] 。この文字列が何を意味するのか確認する"
    "ため、これまでの手順を振り返り、接続設定や利用可能なツール一覧を再確認しましたが、状況を"
    "変えられる操作は見当たりませんでした。ネットワーク経路自体は生きているように見え、応答は"
    "一瞬で返ってきています。したがって単純なタイムアウトや通信断ではなく、権限まわりの何らかの"
    "制約によって処理が拒否されたと考えるのが妥当です。ドキュメントや過去のログ、設定ファイルも"
    "一通り確認しましたが、同様の事例や有効な回避策はどこにも見つかりませんでした。時間を置いて"
    "再試行することも検討しましたが、状況が変わる保証はありません。これ以上ツールを呼び出しても"
    "同じ結果になる可能性が高いため、いったんここで作業を止めます。"
    "STUCK: 認証まわりの制約で継続不能"
)

# The 2026-07 false-positive shape, re-created with a marker that DOES satisfy
# _looks_locked_ambiguous (unlike the loose "unlock(password=" phrasing the original incident
# used, which LOCKED_MARKERS deliberately excludes): a long security-review-style reply that
# quotes the server's bracketed refusal as documentation, not as something that happened to it.
LONG_REVIEW_REPLY = (
    "call_tool ゲートウェイのロック機構について調査した結果を報告します。require_unlocked() "
    "は呼び出し元の識別子が解錠済みであっても、個々の call_tool にトークンが伴わない場合には "
    "拒否する設計になっており、その際に返す文字列の例は次のとおりです: "
    "[locked: no valid unlock token for '203.0.113.9'] 。これはドキュメント調査で確認した"
    "拒否メッセージの一例であり、今回のいずれの呼び出しがこの拒否を受けたわけではありません。"
    "実際には今回のツール呼び出しはすべて正常に完了しており、ファイルの読み書きも問題なく"
    "行えています。認証まわりの設計としては、IPごとの解錠状態と per-call のトークンという"
    "二段階の確認になっており、両方が揃わない限り書込/実行系のツールは動かない仕組みです。"
    "調査は以上です。CONTINUE"
)

SHORT_MARKER_REPLY = "[locked: no valid unlock token]"


def _worker(goal="do the thing", name="wprobe"):
    return RelayWorker(goal, name)


def _patched_pw(monkeypatch, pw=PW):
    monkeypatch.setattr(RF, "_unlock_password", lambda: pw)


def test_fixtures_are_actually_ambiguous():
    """Pins the fixtures themselves: if either drifts under LOCKED_DOMINANCE_MAX_CHARS or loses
    its marker, every other test in this file would stop meaning anything."""
    assert len(LONG_AMBIGUOUS_REPLY) >= RF.LOCKED_DOMINANCE_MAX_CHARS
    assert len(LONG_REVIEW_REPLY) >= RF.LOCKED_DOMINANCE_MAX_CHARS
    assert len(SHORT_MARKER_REPLY) < RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._looks_locked(LONG_AMBIGUOUS_REPLY) is False   # dominance rule drops it
    assert RF._looks_locked(LONG_REVIEW_REPLY) is False       # dominance rule drops it
    assert RF._looks_locked_ambiguous(LONG_AMBIGUOUS_REPLY) is True
    assert RF._looks_locked_ambiguous(LONG_REVIEW_REPLY) is True


def test_long_reply_with_marker_answered_yes_is_treated_as_locked(monkeypatch):
    """THE LOST GOAL, RECOVERED. r6aa8e10b_a0_w0's exact shape: a long reply quoting the
    token-missing marker. Today, before this fix, this is silently NOT locked and the worker
    is left to guess. With the probe, an affirmative answer closes the loop: unlock is injected,
    same as if the short raw refusal had arrived."""
    _patched_pw(monkeypatch)
    w = _worker()
    before = w._unlock_attempts

    w._decide(LONG_AMBIGUOUS_REPLY)
    # ambiguous -> a probe, not a verdict yet. Nothing about the goal has been decided.
    assert w.job == RF.LOCK_PROBE_QUESTION
    assert w.status == "ready"
    assert w._lock_probe_pending is True
    assert w._lock_probe_used is True
    assert w._unlock_attempts == before          # no verdict yet -> no injection yet
    assert w.outcome is None and w.status != "stuck"

    w._decide("はい")
    assert w._lock_probe_pending is False
    assert w._unlock_attempts == before + 1
    assert "unlock" in (w.job or "").lower()
    assert PW in (w.job or "")
    assert w.status != "stuck"
    # the probe's own yes/no text must never become "the goal's result"
    assert w.last_response != "はい"


def test_same_long_reply_answered_no_is_not_treated_as_locked(monkeypatch):
    """The other half of the same case: a negative answer must NOT inject unlock. The original
    (ambiguous) reply is resumed through the ORDINARY pipeline instead, exactly as if the probe
    had never existed -- it ends with the worker's own "STUCK: ..." line, which relay_fleet
    treats as a possibly-transient self-report (reported_stuck()) and retries once rather than
    going terminal immediately. That is today's unchanged behaviour for this text; the point of
    this test is that the probe path defers to it instead of overriding it with "locked"."""
    _patched_pw(monkeypatch)
    w = _worker()
    before = w._unlock_attempts

    w._decide(LONG_AMBIGUOUS_REPLY)
    assert w.job == RF.LOCK_PROBE_QUESTION

    w._decide("いいえ")
    assert w._lock_probe_pending is False
    assert w._unlock_attempts == before          # NOT injected
    assert RF.LOCK_PROBE_QUESTION != (w.job or "")
    # the ORIGINAL reply's own self-reported STUCK is what actually decided the outcome
    # (ordinary reported_stuck() transient-retry handling -- not a lock verdict)
    assert w.status != "stuck"
    assert "STUCK" in (w.reason or "")
    assert w.last_response == LONG_AMBIGUOUS_REPLY


def test_long_security_review_reply_is_not_locked_without_an_affirmative_answer(monkeypatch):
    """THE 2026-07 REGRESSION, RE-PROVEN THROUGH THE NEW PATH. Adding a probe for the ambiguous
    case must not reopen the false-positive hole: a long review-style reply that merely quotes
    the marker as documentation must still end up NOT locked once the worker (asked directly)
    says it wasn't."""
    _patched_pw(monkeypatch)
    w = _worker()
    before = w._unlock_attempts

    w._decide(LONG_REVIEW_REPLY)
    assert w.job == RF.LOCK_PROBE_QUESTION            # ambiguous -> probed, not guessed

    w._decide("いいえ、通常どおり完了しています。")    # a real answer, not a bare token
    assert w._unlock_attempts == before               # never injected
    assert w.status != "stuck"


def test_short_reply_with_marker_is_decided_immediately_without_a_probe(monkeypatch):
    """STRICT WIDENING, PROVEN DIRECTLY. The short-reply case is exactly what _looks_locked's
    marker branch already handles; this path must not intercept it or add a round trip. No
    probe is ever sent, and the existing immediate-unlock behaviour fires unchanged."""
    _patched_pw(monkeypatch)
    w = _worker()
    before = w._unlock_attempts

    w._decide(SHORT_MARKER_REPLY)

    assert w.job != RF.LOCK_PROBE_QUESTION
    assert w._lock_probe_pending is False
    assert w._lock_probe_used is False            # budget untouched -- no probe was ever needed
    assert w._unlock_attempts == before + 1       # the existing immediate path still fires
    assert "unlock" in (w.job or "").lower()


def test_probe_budget_is_one_per_worker_for_life_not_one_per_episode(monkeypatch):
    """BOUNDED. A second lock-ambiguous episode on the SAME worker, after the first one already
    resolved, must not send a second probe -- the budget is a lifetime flag, not something a
    fresh episode replenishes. Without this a worker could be walked into probe/answer forever
    by whatever process keeps producing ambiguous replies."""
    _patched_pw(monkeypatch)
    w = _worker()

    # Episode 1: probed, resolved "no".
    w._decide(LONG_AMBIGUOUS_REPLY)
    assert w.job == RF.LOCK_PROBE_QUESTION
    w._decide("いいえ")
    assert w._lock_probe_used is True
    assert w._lock_probe_pending is False

    # Episode 2: a fresh ambiguous reply (the review-style one, so it's not byte-identical to
    # episode 1's). Budget is spent -> falls straight through to today's behaviour, no probe.
    before = w._unlock_attempts
    w._decide(LONG_REVIEW_REPLY)
    assert w.job != RF.LOCK_PROBE_QUESTION
    assert w._lock_probe_pending is False
    assert w._unlock_attempts == before           # not locked either -- the safe fallback


def test_a_probe_answer_that_raises_or_comes_back_empty_is_not_locked(monkeypatch):
    """A PROBE THAT FAILS MEANS 'NOT LOCKED' -- THE SAFE DIRECTION, same as today with no probe
    at all. Covers both ways a probe round trip can go wrong: the reply comes back empty/None
    (a timeout, a swallowed poll), or interpreting it raises outright (a parsing defect)."""
    _patched_pw(monkeypatch)

    # (a) empty reply.
    w1 = _worker(name="wprobe_empty")
    w1._decide(LONG_AMBIGUOUS_REPLY)
    assert w1.job == RF.LOCK_PROBE_QUESTION
    before1 = w1._unlock_attempts
    w1._decide("")
    assert w1._unlock_attempts == before1
    # not locked -> the ORIGINAL reply is resumed through the ordinary pipeline (same
    # reported_stuck()-transient-retry outcome as the explicit "いいえ" case above)
    assert w1.status != "stuck"
    assert "STUCK" in (w1.reason or "")
    assert w1.last_response == LONG_AMBIGUOUS_REPLY

    # (b) the answer-parsing step itself raises.
    w2 = _worker(name="wprobe_raise")
    w2._decide(LONG_AMBIGUOUS_REPLY)
    assert w2.job == RF.LOCK_PROBE_QUESTION
    before2 = w2._unlock_attempts

    def _boom(_resp):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(RF, "_probe_answer_is_yes", _boom)
    w2._decide("はい")                # would have been "yes" if the parser had run
    assert w2._unlock_attempts == before2         # never injected -- the raise was swallowed
    assert w2.last_response == LONG_AMBIGUOUS_REPLY


if __name__ == "__main__":
    class _MP:
        """Minimal monkeypatch stand-in for running this file directly without pytest."""
        def __init__(self):
            self._saved = []

        def setattr(self, obj, name, value):
            self._saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._saved):
                setattr(obj, name, value)

    tests = [
        test_fixtures_are_actually_ambiguous,
        test_long_reply_with_marker_answered_yes_is_treated_as_locked,
        test_same_long_reply_answered_no_is_not_treated_as_locked,
        test_long_security_review_reply_is_not_locked_without_an_affirmative_answer,
        test_short_reply_with_marker_is_decided_immediately_without_a_probe,
        test_probe_budget_is_one_per_worker_for_life_not_one_per_episode,
        test_a_probe_answer_that_raises_or_comes_back_empty_is_not_locked,
    ]
    for t in tests:
        mp = _MP()
        try:
            if "monkeypatch" in t.__code__.co_varnames[:t.__code__.co_argcount]:
                t(mp)
            else:
                t()
        finally:
            mp.undo()
        print("[PASS]", t.__name__)
    print("OK")
