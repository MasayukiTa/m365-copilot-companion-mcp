"""A validation matrix for the delivery detector, because correlation was read as proof.

THE CRITICISM THIS ANSWERS. The detector was justified like this: across twenty turns, all
eleven passes had the marker in the conversation and eight of the nine failures did not, and
the check abstained on none of them -- therefore an absent marker means the turn was never
delivered, therefore those turns leave the capability denominator, therefore capability is
0.917 rather than 0.579. Every step was measured on the same twenty observations it then
re-scored, and "abstained on none" measures whether the check ANSWERS, not whether the
answers are RIGHT. A detector selected because it removes failures, and validated only on the
failures it removes, will keep removing them. The capability figure is withdrawn.

WHAT MATTERS IS THE FALSE NEGATIVE. A false "delivered" costs one misgraded row. A false "not
delivered" deletes a real failure from the denominator and inflates the headline, so every row
below that constructs a DELIVERED turn and tries to make it look absent is worth more than any
number of rows where the detector agrees with itself.

AND A SECOND ROUND OF REVIEW KILLED THE FIRST ANSWER TO THIS. Rotation was to be caught by
pinning /history to the conversation URL captured before the turn. On this target that cannot
work: the bridge records, from a live probe, that page.url carries no conversation identifier
and does not change even when a sidebar click visibly switches conversations. Two different
conversations compared EQUAL, so the check could never fire, and pointing /history at that URL
could navigate the page away from the conversation being inspected. The identity now comes
from the conversation's own contents -- fingerprinted before the turn, required to still be
there after it -- which needs no URL at all.

The remaining known false negative is written down rather than left to be discovered: if the
conversation renders the prompt without the marker, this reports a delivered turn as absent.
"""
import json

import pytest

from bench.companionbench import agents as A


NONCE = "cb-turn-deadbeef0000"


def _http(payload):
    return "HTTP/1.1 200 OK\r\n\r\n" + json.dumps(payload)


def _user(text):
    return {"role": "user", "text": text}


def _ours(text=""):
    """A message this adapter could have sent: it carries the marker prefix."""
    return _user("%s\n\n[%s-%s]" % (text or "a task", A.BridgeAgent.NONCE_PREFIX, "old00000"))


DELIVERED = _user("do the task\n\n[%s]" % NONCE)


class _Bridge(A.BridgeAgent):
    """A BridgeAgent whose only contact with the world is a scripted `_request`.

    `history` is a list of payloads consumed in order (the last one repeats), which is how
    hydration lag is expressed: the same request, answered differently the second time.
    """

    def __init__(self, history):
        A.BridgeAgent.__init__(self)
        self._history = list(history)
        self.asked = []

    def _request(self, path, timeout=None):
        self.asked.append(path)
        if path.startswith("/history"):
            payload = self._history[0] if len(self._history) == 1 else self._history.pop(0)
            return _http(payload)
        raise AssertionError("unscripted request: %s" % path)


def _run(history, anchor):
    b = _Bridge(history)
    b.HISTORY_RETRY_S = 0
    return b._confirm_delivered(NONCE, anchor)


def _anchor_of(*messages):
    return [A.BridgeAgent._digest(m) for m in messages if m.get("role") == "user"]


# -- delivered turns that must NOT be reported as absent ----------------------------------

def test_a_truncated_capture_is_not_a_negative():
    """The bridge scrolls from the top and stops at a bound, so it drops the NEWEST turn.

    Reporting "absent" from a record known to be incomplete promotes "not observed" to "never
    happened", and does it exactly where the evidence is weakest.
    """
    prior = _user("an older turn")
    got = _run([{"ok": True, "url": "u", "truncated": True, "captured": 10,
                 "messages": [prior]}], _anchor_of(prior))
    assert got["delivered"] is None
    assert "incompletely" in got["why"]


def test_a_rotated_conversation_is_not_a_negative():
    """Delivered into A; by the time we look the page shows B.

    B legitimately lacks the marker. Caught by CONTENTS -- the messages that were there before
    the turn are not there now -- because the URL cannot tell these two apart on this target.
    """
    prior = _user("turn one of conversation A")
    other = _user("something from conversation B")
    got = _run([{"ok": True, "url": "u", "messages": [other]}], _anchor_of(prior))
    assert got["delivered"] is None
    assert "the page moved" in got["why"]


def test_a_fresh_conversation_showing_someone_elses_messages_is_not_a_negative():
    """The empty-anchor case, which is the one an anchor cannot catch.

    An empty anchor is a prefix of every conversation, so rotation away from a FRESH one would
    pass the contents check. It is therefore checked the other way round: in a conversation
    opened for this episode, every message must be one this adapter could have sent.
    """
    got = _run([{"ok": True, "url": "u", "messages": [_user("unrelated, from elsewhere")]}], [])
    assert got["delivered"] is None
    assert "did not send" in got["why"]


def test_an_unhydrated_view_is_retried_and_then_seen():
    """Empty first, populated second. The old loop stopped at the first `ok` and said no."""
    got = _run([{"ok": True, "url": "u", "messages": []},
                {"ok": True, "url": "u", "messages": [DELIVERED]}], [])
    assert got["delivered"] is True


def test_a_view_that_never_populates_abstains_rather_than_denying():
    got = _run([{"ok": True, "url": "u", "messages": []}], [])
    assert got["delivered"] is None
    assert "un-hydrated" in got["why"]


def test_a_turn_that_could_not_be_fingerprinted_beforehand_abstains():
    """No anchor means no way to tell this view from another one. That is not a negative.

    `/history` needs the page lock and can answer busy, so this is the ordinary case under
    load -- which is also when rotation is likeliest. The previous design fell back to an
    UNPINNED check here and returned a definite negative, quietly restoring the bug it
    claimed to have fixed, on exactly the runs where it mattered most.
    """
    got = _run([{"ok": True, "url": "u", "messages": [_user("whatever")]}], None)
    assert got["delivered"] is None
    assert "fingerprinted" in got["why"]


def test_a_delivered_turn_with_a_bad_answer_is_not_excluded():
    """THE ROW THE WHOLE INSTRUMENT RESTS ON.

    If a capability failure can be misread as non-delivery, every capability figure is
    selected upward. A wrong answer changes what the companion SAID; it does not remove the
    request from the conversation.
    """
    got = _run([{"ok": True, "url": "u",
                 "messages": [DELIVERED,
                              {"role": "assistant", "text": "I edited the wrong file"}]}], [])
    assert got["delivered"] is True


def test_a_delivered_turn_whose_reply_navigated_is_not_excluded():
    """A failure that navigates must not arrange its own exclusion.

    The marker is found, so delivery is settled regardless of where the page ended up: a
    positive needs no anchor, because nothing but this turn could have put the marker there.
    """
    got = _run([{"ok": True, "url": "somewhere-else", "messages": [DELIVERED]}],
               _anchor_of(_user("a prior turn that is now gone")))
    assert got["delivered"] is True


def test_a_busy_bridge_abstains_rather_than_denying():
    got = _run([{"ok": False, "error": "busy"}], [])
    assert got["delivered"] is None


# -- turns that SHOULD be reported as absent ----------------------------------------------

def test_an_undelivered_turn_in_the_same_conversation_is_a_negative():
    """The case this exists for, and the only shape that earns a negative.

    The conversation still holds exactly what it held before the turn, so it is demonstrably
    the same view; the marker is demonstrably not in it.
    """
    prior = _ours("the previous turn")
    got = _run([{"ok": True, "url": "u",
                 "messages": [prior, {"role": "assistant", "text": "How can I help?"}]}],
               _anchor_of(prior))
    assert got["delivered"] is False
    assert "still holds the messages it had before" in got["why"]


def test_a_stripped_marker_is_a_known_false_negative_and_is_recorded_as_one():
    """A LIMIT, NOT A PASS.

    If the conversation renders the prompt without the marker -- trimmed, rewritten, collapsed
    -- this reports a delivered turn as absent, and nothing available here distinguishes the
    two. The test exists so the limit is measured rather than discovered later, and so that
    anyone changing this code is told what it still gets wrong.
    """
    prior = _ours("the previous turn")
    got = _run([{"ok": True, "url": "u", "messages": [prior, _user("do the task")]}],
               _anchor_of(prior))
    assert got["delivered"] is False, "known limitation: an unmarked prompt reads as absent"


# -- the requirement, not the branches ----------------------------------------------------

def test_no_url_is_ever_sent_to_history():
    """Pinning by URL was inoperative here AND could navigate the page away mid-check.

    Asserted against the requests actually made, so reintroducing the pin fails this test
    even if every branch above still passes.
    """
    b = _Bridge([{"ok": True, "url": "u", "messages": [DELIVERED]}])
    b.HISTORY_RETRY_S = 0
    b._confirm_delivered(NONCE, [])
    assert b.asked and all(p == "/history" for p in b.asked), b.asked


def test_the_turn_path_fingerprints_before_sending_and_anchors_the_check():
    """END TO END THROUGH `__call__`, because every branch test above injects the anchor.

    A detector that is correct in isolation and never anchored in production is not a
    detector, and that gap is exactly what the previous matrix could not see.
    """
    seen = {}

    class _Turn(A.BridgeAgent):
        def __init__(self):
            A.BridgeAgent.__init__(self)
            self.calls = []

        def _request(self, path, timeout=None):
            self.calls.append(path)
            if path.startswith("/history"):
                if len(self.calls) == 1:          # the fingerprint, before the turn
                    return _http({"ok": True, "url": "u", "messages": [_ours("earlier")]})
                return _http({"ok": True, "url": "u",
                              "messages": [_ours("earlier"), _user("no marker here")]})
            return "HTTP/1.1 200 OK\r\n\r\ndata: hello\n\nevent: done\n\n"

    b = _Turn()
    b.fresh_conversation = False
    b.HISTORY_RETRY_S = 0
    b("do the task", "C:/wd")
    entry = b.transcript[-1]
    seen["anchored"] = entry["anchored"]
    assert seen["anchored"] is True, "the turn must fingerprint the conversation first"
    assert b.calls[0] == "/history", "the fingerprint must be taken BEFORE the send"
    assert entry["prompt_in_conversation"] is False
    assert entry["anchor_cost_s"] >= 0, "the harness's own overhead is on the record"
