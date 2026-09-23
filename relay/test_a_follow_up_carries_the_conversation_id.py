# -*- coding: utf-8 -*-
"""A follow-up found its conversation by matching the GOAL TEXT, and the id was there all along.

WHAT THIS IS ABOUT. Continuing a finished fleet task means sending the next instruction into the
conversation that did the work. The fleet supports that: `driver_for(conversation_id=...)`
continues an existing Copilot conversation, measured with a control. What decided WHICH
conversation was `socket_route.conversation_for_goal(goal_text)` -- a lookup keyed on the words
of the original goal.

Identity by wording fails in the ordinary case, not the exotic one: re-run a goal and the newest
wins, re-phrase it and nothing matches, and the fleet prints "follow_up_to had no recorded
conversation; starting a fresh one" and opens a chat that never heard the question.

AND THE ID WAS NOT MISSING. Every fleet transcript carries a `guid` line, the chat window reads
it and stores `ConvUrl = "sess:<guid>"`, and `relay_fleet._conversation_id_or_empty` accepts
exactly that shape. docs/incidents/20260912_a_fleet_conversation_could_be_read_and_never_
answered.md had already written the sentence this file exists to enforce -- *"The identity
existed and was durable the whole time. Nothing copied it into the field every consumer reads
... the ability was built and nothing ever asked"* -- and recorded that what looked like
continuing through the fleet was "a new conversation with the context re-pasted by hand".

That re-pasting is `relay/project_memory.py`, which primes a digest of past themes into every
goal. It grew to 216 themes, 36% of them one-shot questions, because it is standing in for a
resume that was not happening.
"""
from __future__ import annotations

import ast
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")
#: Where the follow-up payload is built since 2026-09-24. ui/test_the_chat_window_sends_what_was_typed.py
#: RUNS it (and reads what it wrote back through fleet_runner.goals_from_command); the two
#: shape checks below stay because they name the order of preference.
SEND = os.path.join(REPO, "ui", "ChatSend.cs")
FOLLOW_UP = "FleetSend DecideFleetSend(string text, string live, Conversation c, int lang)"
FLEET = os.path.join(REPO, "relay", "relay_fleet.py")


def _cs_method(signature, path=CHAT):
    """One C# method's text, located by its own signature rather than by a line inside it."""
    src = io.open(path, encoding="utf-8").read()
    i = src.index(signature)
    j = src.find("\n    void ", i + 1)
    k = src.find("\n    string ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)] if ends else src[i:]


# ── the sender hands over the id ──────────────────────────────────────────────────────────

def test_a_follow_up_sends_the_conversation_id():
    """THE DEFECT. The goal dict carried only `follow_up_to`, the goal TEXT."""
    body = _cs_method(FOLLOW_UP, path=SEND)
    assert 'g["resume_conv"]' in body, (
        "the follow-up no longer carries resume_conv, so the fleet is back to identifying the "
        "conversation by matching the wording of the original goal")
    i = body.index('g["resume_conv"]')
    assert "c.ConvUrl" in body[max(0, i - 300):i + 120], (
        "resume_conv is set from something other than the row's own conversation reference")


def test_the_row_it_reads_is_the_one_that_holds_the_id():
    """`ConvUrl` on a fleet row is "sess:<guid>", read from the transcript's guid line. If that
    stops being true the id above is a string nobody can resolve."""
    src = io.open(CHAT, encoding="utf-8").read()
    assert 'ConvUrl = guid.Length > 0 ? "sess:" + guid : ""' in src, (
        "fleet rows no longer carry sess:<guid>; the follow-up has nothing to send")


def test_the_text_lookup_is_kept_as_a_fallback_and_not_as_the_route():
    """Removing it would break rows captured before the guid was recorded. Keeping it as the
    FIRST answer is what this file forbids."""
    body = _cs_method(FOLLOW_UP, path=SEND)
    assert 'g["follow_up_to"]' in body, "the fallback for a row with no guid is gone"
    assert body.index('g["resume_conv"]') < body.index('g["follow_up_to"]'), (
        "the id must be offered before the text fallback")


# ── the fleet prefers the id, and says which door it used ─────────────────────────────────

def test_the_fleet_only_matches_by_text_when_no_id_was_given():
    """Asserted on the guard itself. `resume_conv` arriving in the goal dict must short-circuit
    the lookup -- otherwise a supplied id and a guessed one are indistinguishable."""
    src = io.open(FLEET, encoding="utf-8").read()
    tree = ast.parse(src, filename="relay_fleet.py")
    # THE INNERMOST `if` THAT CONTAINS THE CALL, not every ancestor of it. The first version
    # walked all If nodes whose SOURCE SEGMENT mentioned conversation_for_goal, which includes
    # the enclosing transport branch forty lines up -- so it failed on a guard that was correct,
    # naming a line that has nothing to do with resuming.
    # THE `if` WHOSE OWN TEST GUARDS THE CALL, found from the call rather than from the text.
    # Two earlier drafts got this wrong in the same way: they collected every If whose SOURCE
    # SEGMENT mentions conversation_for_goal, which includes the transport branch forty lines
    # up, and "smallest span" picked that one too because the guard is nested inside it. What
    # identifies the guard is that the CALL is in its body, not that the call is somewhere
    # beneath it.
    call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "conversation_for_goal"):
            call = node
            break
    assert call, "the goal-text lookup is gone entirely -- re-derive what this file claims"

    guards = [n for n in ast.walk(tree)
              if isinstance(n, ast.If)
              and any(call is c for b in n.body for c in ast.walk(b))]
    assert guards, "the goal-text lookup is no longer guarded at all"
    innermost = min(guards, key=lambda n: (n.end_lineno or 0) - n.lineno)
    first = (ast.get_source_segment(src, innermost) or "").splitlines()[0]
    assert "not self.resume_conv" in first, (
        "the goal-text lookup runs even when a conversation id was handed in:\n%s" % first)


def test_which_door_was_used_is_recorded():
    """A fortnight of "continuing through the fleet works" was true of neither door, and nothing
    in the record told them apart. An id resume and a text match must be distinguishable in the
    log without reading the code."""
    src = io.open(FLEET, encoding="utf-8").read()
    assert "resuming by id" in src
    assert "matched a conversation by goal TEXT" in src


def test_the_id_shape_the_sender_uses_is_the_one_the_fleet_accepts():
    """The two halves are in different languages and different files; this is the seam.
    `_conversation_id_or_empty` is deliberately strict -- it takes a bare guid or `sess:<guid>`
    and refuses a URL, because pulling a guid out of a URL turns a tab resume into a socket
    resume and looks identical when it works."""
    from relay.relay_fleet import _conversation_id_or_empty

    guid = "1c34d9cf-508f-47dd-9c6a-337f9b9ca283"
    assert _conversation_id_or_empty("sess:" + guid) == guid, (
        "the shape the chat window sends is not accepted by the fleet")
    assert _conversation_id_or_empty(guid) == guid
    assert _conversation_id_or_empty("https://example.invalid/chat/" + guid) == "", (
        "a URL is being read as a socket conversation id")
    assert _conversation_id_or_empty("") == ""
    assert _conversation_id_or_empty(None) == ""
