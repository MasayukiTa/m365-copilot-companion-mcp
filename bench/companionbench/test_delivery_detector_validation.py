"""A held-out matrix for the delivery detector, because correlation was being read as proof.

THE CRITICISM THIS ANSWERS. The detector was justified like this: across twenty turns, all
eleven passes had the marker in the conversation and eight of the nine failures did not, and
the check abstained on none of them -- therefore an absent marker means the turn was never
delivered, therefore those turns leave the capability denominator, therefore capability is
0.917 rather than 0.579. Every step of that was measured on the same twenty observations it
then re-scored, and "abstained on none" measures whether the check ANSWERS, not whether the
answers are right. A detector selected because it removes failures, validated only on the
failures it removes, is a instrument tuned to flatter itself.

What was missing is the case that would refute it: a turn that WAS delivered, made to look
absent. Every row below is one, constructed rather than sampled, so the detector's negative
verdict can be checked against a ground truth this file sets. It is the false-POSITIVE
direction that matters -- a false "delivered" costs one misgraded row, a false "not delivered"
silently deletes a real failure from the denominator and inflates the headline.

The matrix, and what each row is worth:

  delivered + truncated capture      -> must NOT be a negative   (the scrape stops early and
                                        it scrolls from the top, so the newest turn is the
                                        likeliest to be missing)
  delivered + conversation rotated   -> must NOT be a negative   (a failure that navigates
                                        would otherwise arrange its own exclusion)
  delivered + view not yet hydrated  -> must NOT be a negative   (empty early, populated late)
  delivered + marker stripped        -> IS a negative, knowingly (nothing distinguishes it
                                        from an undelivered turn; recorded as a known limit)
  genuinely undelivered + canned reply -> IS a negative          (the case it exists for)
  delivered + a bad answer           -> must NOT be a negative   (capability failures must
                                        stay in the denominator; this is the whole risk)
"""
import json

import pytest

from bench.companionbench import agents as A


NONCE = "cb-turn-deadbeef0000"
CONV_A = "https://example.invalid/chat/conversation-aaa"
CONV_B = "https://example.invalid/chat/conversation-bbb"


def _http(payload):
    return "HTTP/1.1 200 OK\r\n\r\n" + json.dumps(payload)


class _Bridge(A.BridgeAgent):
    """A BridgeAgent whose only contact with the world is a scripted `_request`.

    `responses` maps a path prefix to either a payload or a list of payloads consumed in
    order, which is how hydration lag is expressed: the same request, answered differently
    the second time.
    """

    def __init__(self, responses):
        A.BridgeAgent.__init__(self)
        self._responses = responses
        self.asked = []

    def _request(self, path, timeout=None):
        self.asked.append(path)
        for prefix, value in self._responses.items():
            if path.startswith(prefix):
                if isinstance(value, list):
                    return _http(value.pop(0) if len(value) > 1 else value[0])
                return _http(value)
        raise AssertionError("unscripted request: %s" % path)


def _verdict(responses, conversation_url=CONV_A):
    b = _Bridge(responses)
    b.HISTORY_RETRY_S = 0
    return b._confirm_delivered(NONCE, conversation_url)


def _user(text):
    return {"role": "user", "text": text}


DELIVERED = [_user("do the task\n\n[%s]" % NONCE)]


# -- the rows that must NOT produce a negative -------------------------------------------

def test_a_truncated_capture_is_not_a_negative():
    """The bridge scrolls from the top and stops at a bound, so it drops the NEWEST turn.

    Reporting "absent" from a record that is known to be incomplete promotes "not observed"
    to "never happened" -- and does it exactly where the evidence is weakest.
    """
    got = _verdict({"/history": {"ok": True, "url": CONV_A, "truncated": True,
                                 "captured": 10, "messages": [_user("an older turn")]}})
    assert got["delivered"] is None
    assert "incompletely" in got["why"]


def test_a_rotated_conversation_is_not_a_negative():
    """Delivered into A; by the time we look the page is on B. B legitimately lacks it."""
    got = _verdict({"/history": {"ok": True, "url": CONV_B, "messages": [_user("hello")]}})
    assert got["delivered"] is None
    assert "different conversation" in got["why"]


def test_an_unhydrated_view_is_retried_and_then_abstains():
    """Empty first, populated second. The old loop stopped at the first `ok` and said no."""
    got = _verdict({"/history": [{"ok": True, "url": CONV_A, "messages": []},
                                 {"ok": True, "url": CONV_A, "messages": DELIVERED}]})
    assert got["delivered"] is True


def test_an_empty_conversation_that_never_populates_abstains_rather_than_denying():
    got = _verdict({"/history": {"ok": True, "url": CONV_A, "messages": []}})
    assert got["delivered"] is None
    assert "empty" in got["why"]


def test_a_delivered_turn_with_a_bad_answer_is_not_excluded():
    """THE ROW THE WHOLE INSTRUMENT RESTS ON.

    If a capability failure can be misread as non-delivery, every reported capability figure
    is selected upward. A wrong answer changes what the companion SAID; it does not remove
    the request from the conversation.
    """
    got = _verdict({"/history": {"ok": True, "url": CONV_A,
                                 "messages": DELIVERED + [{"role": "assistant",
                                                           "text": "I edited the wrong file"}]}})
    assert got["delivered"] is True


# -- the rows that SHOULD produce a negative ---------------------------------------------

def test_an_undelivered_turn_with_a_canned_reply_is_a_negative():
    """The case actually observed: a greeting comes back and the request is nowhere."""
    got = _verdict({"/history": {"ok": True, "url": CONV_A,
                                 "messages": [_user("earlier, unrelated"),
                                              {"role": "assistant", "text": "How can I help?"}]}})
    assert got["delivered"] is False


def test_a_stripped_marker_is_a_known_false_negative_and_is_documented_as_one():
    """A LIMIT, NOT A PASS.

    If the conversation renders the prompt without the marker -- trimmed, rewritten, collapsed
    -- this reports a delivered turn as absent and nothing here can tell the difference. The
    test exists so the limit is written down and measured rather than discovered later.
    """
    got = _verdict({"/history": {"ok": True, "url": CONV_A,
                                 "messages": [_user("do the task")]}})
    assert got["delivered"] is False, "known limitation: an unmarked prompt reads as absent"


# -- the check must not fail the run it is checking ---------------------------------------

def test_a_busy_bridge_abstains_rather_than_denying():
    got = _verdict({"/history": {"ok": False, "error": "busy"}})
    assert got["delivered"] is None


def test_the_conversation_is_pinned_to_the_one_the_turn_was_sent_to():
    b = _Bridge({"/history": {"ok": True, "url": CONV_A, "messages": DELIVERED}})
    b.HISTORY_RETRY_S = 0
    b._confirm_delivered(NONCE, CONV_A)
    assert any("url=" in p for p in b.asked), "history must be asked about a named conversation"


def test_without_a_known_conversation_it_does_not_invent_a_rotation():
    """`/conv` can answer busy. An unpinned check is weaker, not wrong -- it must not deny."""
    got = _verdict({"/history": {"ok": True, "url": CONV_B, "messages": DELIVERED}},
                   conversation_url="")
    assert got["delivered"] is True


@pytest.mark.parametrize("same,left,right", [
    (True, CONV_A, CONV_A + "?tenant=x"),
    (True, CONV_A + "#frag", CONV_A),
    (True, CONV_A + "/", CONV_A),
    (False, CONV_A, CONV_B),
])
def test_conversation_identity_ignores_query_and_fragment(same, left, right):
    """The SPA rewrites query strings freely; a raw comparison would see a rotation always."""
    assert A.BridgeAgent._same_conversation(left, right) is same
