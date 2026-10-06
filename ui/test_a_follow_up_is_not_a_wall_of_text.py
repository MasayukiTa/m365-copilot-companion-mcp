# -*- coding: utf-8 -*-
"""A one-sentence follow-up reached the worker as a wall, and no instruction that size is testable.

WHAT THE OPERATOR SAW on 2026-09-14: the prompt a fleet worker received contained, in one block,
the original goal, an earlier follow-up, a five-step editing procedure with hashes and library
calls, and a verification request. The follow-up that produced it was one sentence.

WHY IT PILED UP. `BuildContinueGoal` prepends the PRIOR GOAL to every continuation, and its own
comment gave the reason: "There is NO stable reopenable URL for this agent, so we do NOT reopen
the old conversation: instead we PREPEND the prior task's context". That premise is no longer
true -- every fleet transcript carries a guid line, the caller already puts `resume_conv` on the
goal, and ContextTokenLimitExceeded arriving on a resumed conversation is the proof that the
server is holding the history. So the paste was a SECOND copy of context the conversation
already had.

And it compounds. `priorGoal` is the previously ASSEMBLED goal, so each continuation swallows the
last one whole: the block grows with every follow-up, not with the work.

WHY THIS IS NOT COSMETIC. The operator's standing rule is that long prompts go to Copilot only
for bench measurement and coding; everything else must be the length a person types, "これで
なければ検証不可能。長いプロンプトでの検証はすべて不正行為". A mechanism that inflates every
follow-up makes that rule unfollowable no matter who is typing -- so every non-bench verification
run through it was invalid, and the machinery was part of why.
"""
from __future__ import annotations

import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _src():
    return io.open(COCKPIT, encoding="utf-8").read()


def _method(signature):
    src = _src()
    i = src.index(signature)
    j = src.find("\n    // ", i + 1)
    k = src.find("\n    string ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)] if ends else src[i:]


def _code_only(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


# ── the property ──────────────────────────────────────────────────────────────────────────

def test_a_resumed_conversation_gets_the_follow_up_alone():
    """THE DEFECT. With an id in hand the prior goal must not be pasted again."""
    body = _code_only(_method("string ContinueText(string priorGoal, string followup, string conversationId)"))
    assert "string.IsNullOrEmpty(conversationId)" in body, (
        "the continuation no longer asks whether it is reopening a conversation")
    assert ": followup;" in body, (
        "a resumed continuation is sending something other than just the follow-up")


def test_the_paste_survives_only_where_there_is_nothing_to_reopen():
    """Removing it outright would break a row captured before guids were recorded. It is the
    fallback, not the route."""
    body = _code_only(_method("string ContinueText(string priorGoal, string followup, string conversationId)"))
    assert "BuildContinueGoal(priorGoal, followup)" in body, (
        "the no-id fallback is gone; a conversation that cannot be reopened now gets no context")


def test_no_caller_builds_the_pasted_goal_directly():
    """Both buttons used to call BuildContinueGoal themselves, so a third caller added later
    would reintroduce the pile-up without touching anything this file watches."""
    code = _code_only(_src())
    assert code.count("BuildContinueGoal(") == 2, (
        "BuildContinueGoal is called %d times; it must be reached only through ContinueText "
        "(its own definition plus the one fallback call)" % code.count("BuildContinueGoal("))
    assert code.count("ContinueText(") == 3, (
        "expected the definition plus two call sites, found %d" % code.count("ContinueText("))


def test_the_id_still_travels_with_the_goal():
    """Sending the bare follow-up is only correct BECAUSE resume_conv goes with it. If that key
    stops being attached, the worker gets a sentence with no context at all -- which is worse
    than the wall."""
    code = _code_only(_src())
    assert code.count('gd["resume_conv"] = ') == 2, (
        "a continuation no longer carries resume_conv, so the bare follow-up has nothing to "
        "continue")


def test_the_pasting_branch_is_documented_as_the_fallback():
    """The old comment asserted the conversation could not be reopened, and used that as the
    reason to paste. Leaving that standing would tell the next reader the opposite of what the
    code does.

    ASSERTED ON THE BUILDER'S OWN HEADER, not by grepping the file for the retired sentence: the
    first draft did that and matched the paragraph EXPLAINING why the sentence is retired, which
    is the fourth time in one day a scan has hit its own commentary. What matters is that the
    surviving paste says it is the no-id path."""
    src = _src()
    i = src.index("string BuildContinueGoal(string priorGoal, string followup)")
    header = src[max(0, i - 400):i]
    assert "NO conversation to reopen" in header, (
        "BuildContinueGoal no longer says it is the fallback for a row with no conversation id")
