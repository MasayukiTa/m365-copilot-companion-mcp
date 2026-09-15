# -*- coding: utf-8 -*-
"""A refusal knows which session it belongs to, so the reply's length stops being evidence.

`_looks_locked` had two ways to decide a worker had been refused for lock, and both were
gated on the reply being under LOCKED_DOMINANCE_MAX_CHARS. The comment inside it said why:

    The refusal record is a single global slot with no client identity, so under concurrency
    one caller's refusal colours everyone's reply -- and a long, ordinary answer is exactly
    what the dominance rule exists to exclude. Identity is the real fix and it is not
    available here.

It is available. tools/lock_state.py records the MCP session each refusal arrived on, and
its own docstring says the field exists so a refusal can be joined to the session it belongs
to. Measured 2026-09-15: 36 refusals inside two hours carrying 13 distinct sessions. The
reader was never told, which is the third time in one day that a writer grew a field and the
code keying on it went on guessing -- the others being the gzipped transcripts the sidebar
could not see, and the third refusal string the fleet could not hear.

WHAT THE GUESS COST. Detection on the server's bracketed marker only fires while the agent
pastes the error back verbatim, and it paraphrases instead, because the output discipline in
every turn asks it to. On 2026-09-15 a worker wrote 「screen_look は「no valid unlock token」
で拒否」 -- the words present, the bracket absent, so no marker matched -- and every reply ran
past 400 characters, so the record branch was shut as well. Four turns went into telling it
that a permanent authorisation failure was probably a network blip, until one reply happened
to come in under the cap and let the recovery through. Nothing about that recovery had
anything to do with how long the reply was.

The rule these tests pin: a refusal really happened in this turn's window, exactly one
session produced it, and the reply talks about being locked in any wording. Each half is
weak alone and that is the point -- prose about the unlock API is the 2026-07 false positive,
and a concurrent worker's refusal is the 533-character summary that was misread as a lock.
"""
import time

import pytest

from relay import relay_fleet as RF


@pytest.fixture()
def records(monkeypatch):
    """Stand in for tools.lock_state.matching_records with whatever the test needs."""
    box = {"rows": []}

    class _Stub:
        @staticmethod
        def matching_records(since, now=None):
            return list(box["rows"])

    # PATCH THE ATTRIBUTE, NOT ONLY sys.modules. The code under test does
    # `from tools import lock_state`, which reads the attribute off the already-imported
    # `tools` package -- replacing the sys.modules entry alone leaves that attribute, and the
    # real module answers while the test believes it is driving a stub.
    import sys
    import tools

    monkeypatch.setitem(sys.modules, "tools.lock_state", _Stub)
    monkeypatch.setattr(tools, "lock_state", _Stub, raising=False)
    return box


def _refusal(session, detail="[locked client IP: '10.0.0.1'] Mutating and execution tools"):
    return {"session": session, "detail": detail, "ts": time.time()}


LONG = "あ" * 900


def test_a_paraphrased_refusal_is_recognised_however_long_the_reply_is(records):
    """The case that lost four turns: the words are there, the bracket is not."""
    records["rows"] = [_refusal("sess-A")]
    reply = "screen_look は「no valid unlock token」で拒否。" + LONG
    assert len(reply) > RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._looks_locked(reply, since=time.time() - 30) is True


def test_prose_about_the_unlock_api_is_still_not_a_lock(records):
    """The 2026-07 false positive: a security review that made the relay unlock four times.

    A refusal is standing in the window here, so only the second half of the rule can say no
    -- which is the half that has to carry it, since the first half cannot tell a reviewer
    from a victim.
    """
    records["rows"] = [_refusal("sess-A")]
    review = ("unlock(password='<password>') のプレースホルダとテスト値について、"
              "引数の扱いを説明する。" + ("う" * 900))
    assert RF._looks_locked(review, since=time.time() - 30) is False


def test_several_sessions_refused_at_once_is_the_normal_state_and_still_counts(records):
    """The first version of this branch required exactly one session and so never fired.

    Replayed against the two runs that died on 2026-09-15, with the clock frozen at each
    turn, fourteen refusals sat in the window from several sessions every time -- several
    workers are locked at once, which is what a fleet looks like. A rule that only fires
    when the machine is idle is a rule that never fires.

    What makes dropping the requirement defensible is that the branch BELOW attributes with
    no identity at all, and without even asking whether the reply mentions a lock. Requiring
    identity only for the long replies would hold them to a standard the short ones have
    never met.
    """
    records["rows"] = [_refusal("sess-A"), _refusal("sess-B"), _refusal("sess-C")]
    reply = "screen_look は未解錠で拒否された。" + LONG
    assert RF._looks_locked(reply, since=time.time() - 30) is True


def test_a_reply_that_never_mentions_being_locked_is_not_one(records):
    """The 533-character meeting summary, which a concurrent worker's refusal once claimed."""
    records["rows"] = [_refusal("sess-A")]
    summary = "会議の要点は三つ。まず日程、次に担当、最後に予算。" + ("か" * 900)
    assert RF._looks_locked(summary, since=time.time() - 30) is False


def test_a_context_less_refusal_is_not_evidence_about_this_worker(records):
    """It came from something in-process, which is never a remote worker.

    The existing branch already filters these; the session branch must filter them too, or
    it would read an internal hook's refusal as the worker's own.
    """
    records["rows"] = [_refusal("sess-A", detail=RF.NO_CONTEXT_REFUSAL + " Denied: ...")]
    reply = "screen_look は未解錠で拒否された。" + LONG
    assert RF._looks_locked(reply, since=time.time() - 30) is False


def test_no_refusal_in_the_window_means_no(records):
    """Talking about being locked is not being locked."""
    records["rows"] = []
    reply = "解錠が必要なようだ。" + LONG
    assert RF._looks_locked(reply, since=time.time() - 30) is False


def test_the_short_marker_path_is_untouched(records):
    """Everything that fired before still fires, by the same branch, without the record.

    `since=0` reaches neither the session branch nor the record branch, so a pass here is a
    pass on the marker rule alone.
    """
    records["rows"] = []
    assert RF._looks_locked("[locked client IP: '10.0.0.1'] Mutating and execution tools "
                            "require an unlock.", since=0.0) is True


def test_the_paraphrase_list_never_answers_on_its_own():
    """It is loose on purpose and must never be the whole test.

    A bare mention of the word is the 2026-07 incident; this asserts the loose signal is
    loose, so that a future reader does not promote it to a decision by itself.
    """
    assert RF._mentions_being_locked("未解錠のため実行できない") is True
    assert RF._mentions_being_locked("解錠の仕組みについて説明する") is True
    assert RF._mentions_being_locked("今日の天気は晴れです") is False
