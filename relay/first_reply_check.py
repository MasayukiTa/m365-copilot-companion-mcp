"""Did the first message of a fresh Copilot conversation get ABSORBED?

THE MECHANISM THIS READS. The first message (contract + theme memory + skill + goal) is one
atomic insert into a composer whose editor model may not be attached yet on a fresh chat
(see the comment above the insert in relay/copilot_autopilot_relay.py). When it is lost the
agent answers as if nobody had asked it anything: a greeting, "what would you like me to
do", "your message looks empty". Measured over the stored transcripts, that is the first
reply of roughly one worker in ten in the last week. Nothing looked at the reply, and the
compact-ledger nudge that follows points at "the first message of this conversation", a
message the agent never acted on.

THE CHECK IS ABOUT THE REPLY, NOT THE SEND. A reply that is short AND reads as a greeting,
an offer of help, an ask for the goal, an empty-message complaint or the canned refusal is a
reply from an agent that has no goal. Any sign of work -- a protocol verdict, a tool call, a
code fence, substantial length, the goal's own identifiers echoed back -- clears it. A false
"not absorbed" costs one duplicate send of the first message, so every pattern here was
tuned against the stored transcripts in both directions (see test_first_reply_check.py).

WHAT THIS CANNOT SEE. A reply that looks like work but ignores the goal's constraints. That
is a different failure (the goal arrived and was misread), and re-sending the same text
would not change it.

Pure: no I/O, no state, never raises.
"""
import re

#: A reply at or above this length is work or an argument, not a greeting. Measured: the
#: longest real non-absorbed first reply on record was 134 characters; 300 leaves room for a
#: greeting followed by a short capability list without ever reaching a real answer.
SHORT_REPLY_MAX_CHARS = 300

#: Structured evidence that the agent acted on a goal. Checked BEFORE the patterns, so a
#: "DONE" reply that happens to contain "お手伝い" is never called a greeting.
_WORK_EVIDENCE = re.compile(
    r"\b(?:DONE|CONTINUE|STUCK|INCONCLUSIVE|PLAN_READY|SPLIT|RESEARCH|ANALYZE|NEXT|CONFIDENCE"
    r"|FAIL|PASS)\b|call_tool\b|CloseIntentTool|```|分割",
)

_PLATFORM_ERROR = re.compile(r"エラー コード|Error code", re.I)

#: The verdicts that PROVE the goal landed (the others only fail to disprove it). A caller
#: that wants to stop watching for the failure does so on these.
STRONG_ABSORBED_REASONS = ("work_evidence", "long_reply", "echoes_goal")

#: Real replies of an agent that received nothing, collected from the transcripts. Each group
#: names what the reply is, which is what the recorded reason carries.
_PATTERNS = (
    ("canned_refusal", re.compile(
        r"それに応答できませんでした|I couldn'?t respond to that|I can'?t respond to that"
        r"|担当者へのエスカレーションが構成されていません", re.I)),
    ("empty_message", re.compile(
        r"メッセージが空|空のよう|空でした|appears to be empty|message (?:is|was) empty"
        r"|empty message|特に指示がない", re.I)),
    ("asks_for_goal", re.compile(
        r"(?:目標|目的|タスク|依頼|指示)の?内容を(?:教えて|お知らせ|お聞かせ)"
        r"|どの(?:ファイル|ような).{0,20}(?:教えてください|お知らせください)"
        r"|(?:ご用件|ご依頼)を(?:どうぞ|お知らせ|お聞かせ|書いて)"
        r"|(?:対象|内容)が特定できません"
        r"|(?:が|は)提示されていません|中身がありません"
        r"|what would you like me to|what (?:can|may) i (?:help|do)|how can i (?:help|assist)"
        r"|please (?:provide|tell me|let me know) (?:the |your )?(?:task|goal|request)", re.I)),
    ("greeting", re.compile(
        r"こんにちは|こんばんは|はじめまして|\bGreeting\b|\bHello\b|\bHi there\b|\bHi[,!]", re.I)),
    ("offers_help", re.compile(
        r"お手伝い(?:できる|いたし|しま|が)|お手伝いを|ご用件|お申し付けください", re.I)),
)

#: Identifier-like tokens of the goal: long ASCII runs and path pieces. A short reply that
#: repeats one of them has read the goal.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_./\\:-]{7,}")


def _goal_tokens(goal):
    seen, out = set(), []
    for m in _TOKEN.finditer(goal or ""):
        t = m.group(0).strip(".:/\\-")
        if len(t) >= 8 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= 60:
            break
    return out


def first_reply_absorbed(reply_text, goal=""):
    """(ok, reason). ok is False only when the reply is demonstrably from an agent that never
    received the goal; reason names which pattern (or why it is fine). Never raises."""
    try:
        text = str(reply_text or "").strip()
        if not text:
            # No reply is not evidence either way; the send path owns silence.
            return True, "empty_reply_not_judged"
        if len(text) >= SHORT_REPLY_MAX_CHARS:
            return True, "long_reply"
        if _PLATFORM_ERROR.search(text):
            # Copilot's own structured error: the turn failed, which says nothing about
            # whether the message was absorbed. The throttle / token-limit handlers own it.
            return True, "platform_error"
        if _WORK_EVIDENCE.search(text):
            return True, "work_evidence"
        for tok in _goal_tokens(goal):
            if tok.lower() in text.lower():
                return True, "echoes_goal"
        goal_text = str(goal or "")
        for name, pat in _PATTERNS:
            if pat.search(text):
                # A goal that itself is a greeting / asks for help is answered with one.
                if name != "canned_refusal" and any(
                        pat2.search(goal_text) for n2, pat2 in _PATTERNS
                        if n2 in ("greeting", "offers_help")):
                    return True, "goal_is_conversational"
                return False, name
        return True, "no_signal"
    except Exception:
        return True, "check_failed"
