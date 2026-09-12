# -*- coding: utf-8 -*-
"""A fleet conversation in the chat window could be read and never answered.

MEASURED 2026-09-12, reported by the operator with a screenshot. Typing a follow-up into an
open fleet conversation answers:

    この会話の送信先を特定できません。会話を開き直してください。

and reopening changes nothing, because reopening supplies nothing that was missing.

SendText picks a send target through three doors, and a fleet row holds none of the keys:

    /switch   needs ConvUrl                          -- ""      (never recorded, see below)
    /resume   needs Source=="chat" and a sid in Name -- Source is "fleet", Name is "w0"
    /new      needs Messages.Count == 0              -- the transcript is loaded

so it fell through all three to send_unknown_conv. The same empty ConvUrl also left
_activeFleetUrl empty, so steer mode never armed and RefreshFleetSnapshot returned at its
first line: one missing field, three symptoms.

WHY ConvUrl WAS EMPTY. relay_fleet._capture_url's body sat entirely under
`if self.page is not None`, and a socket worker has no page -- so it never recorded the
conversation it was in. Measured on the live machine that day:

    .fleet/status.json         w0           conv_url ""
    .fleet/conversations.json  267 fleet rows, url "" on all but one
    .fleet/transcripts/*.jsonl keys goal/key/meta/name/role/text/ts/turn -- no `guid`
    .fleet/socket_route.jsonl  worker_done  conv_client d3710cc2-a5ff-4d74-9c0e-027e96773cb9

The identity existed and was durable the whole time; nothing copied it into the field every
consumer reads. (relay/test_socket_conversation_identity.py covers that half.)

THE THREE DOORS WERE THE WRONG DOORS ANYWAY. A fleet conversation does not live on the bridge
page -- it lives in a worker on a socket -- so pulling PAGE onto it would be wrong even if a
URL existed. The fleet already accepts a message for one of its conversations in both states
it can be in: a steer for a live worker, and a goal carrying `follow_up_to` once it is not.
Both had no caller from here.

ADDRESSING, which is the part that can go quietly wrong: a live worker is addressed by NAME,
and "w0" exists in every run there has ever been. So the live branch is taken only when the
CURRENT status.json lists a worker whose TRANSCRIPT PATH is this conversation's -- an identity
join, not a name match. Everything else goes to the follow-up, which is addressed by goal
text and is therefore safe to write at any time.
"""
from __future__ import annotations

import os
import re

import pytest

UI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(UI, "CopilotChat.cs")


def _code() -> str:
    """The source with // comments removed.

    A source assertion that matches this file's own explanatory comments passes whether or not
    the code does anything -- and every comment here names the very identifiers being asserted
    on, so without this the whole module would be vacuous.
    """
    with open(SRC, encoding="utf-8") as fh:
        text = fh.read()
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _block(code: str, start: str, length: int = 2600) -> str:
    i = code.index(start)
    return code[i:i + length]


# ── the send no longer falls through to the refusal ────────────────────────────────────────

def test_a_fleet_conversation_is_routed_before_the_page_doors():
    """It has to be decided BEFORE the pinning block, not inside it: the three doors there are
    about putting the bridge page on a conversation, which is not where this one is."""
    code = _code()
    body = _block(code, "void SendText(")
    fleet = body.index('target.Source == "fleet"')
    doors = body.index("ReferenceEquals(target, _pageConv)")
    assert fleet < doors, (
        "a fleet conversation still reaches the page-pinning doors, where it has no key to any "
        "of them and falls through to send_unknown_conv")
    assert "SendToFleetConversation(target, text)" in body


def test_the_refusal_still_exists_for_what_it_was_actually_for():
    """send_unknown_conv keeps its job. A `chat` conversation with no url, no sid and messages
    genuinely has nowhere to go, and silently sending it somewhere would be worse."""
    code = _code()
    assert 'AddAssistant(T("send_unknown_conv"))' in code


# ── addressing: the part that can go quietly wrong ─────────────────────────────────────────

def test_a_live_worker_is_matched_on_its_transcript_not_its_name():
    """THE ONE THAT WOULD MISDELIVER. deliver_steers routes by worker name, and every run has
    a w0 -- so steering an old conversation by name would interrupt a stranger doing unrelated
    work, and the steer would look delivered."""
    code = _code()
    body = _block(code, "string LiveWorkerFor(")
    assert 'SS(w, "transcript"), c.Transcript' in body, (
        "the live worker is not identified by transcript path, so a name collision across runs "
        "can deliver this message to a different worker")
    assert 'd["running"]' in body, "a stale status.json would name workers that are long gone"


def test_a_terminal_or_pending_worker_is_not_treated_as_live():
    """A steer for a terminal worker is dropped by deliver_steers, and a pending one has no
    turn to take it -- either way the message must go to the follow-up instead."""
    body = _block(_code(), "string LiveWorkerFor(")
    for st in ("done", "resolved", "failed", "error", "cancelled", "stopped", "stuck", "pending"):
        assert '"%s"' % st in body, "status %r is not excluded from the live branch" % st


def test_the_follow_up_names_the_conversation_by_goal_text():
    """`follow_up_to` is resolved by socket_route.conversation_for_goal, which matches on the
    goal text. The Title is truncated for display, so using it would look right and resolve to
    nothing -- and a follow-up that silently starts a fresh conversation answers plausibly."""
    code = _code()
    body = _block(code, "void SendToFleetConversation(")
    assert 'g["follow_up_to"] = goal' in body
    assert "c.Goal" in body and "c.Title" not in body, (
        "the follow-up is addressed by the display title rather than the goal text")


def test_a_conversation_with_no_recorded_goal_says_so_instead_of_guessing():
    """Every transcript written before the identity fix has no goal-resolvable conversation.
    Those must be refused out loud, not turned into a fresh chat wearing a follow-up's words."""
    body = _block(_code(), "void SendToFleetConversation(")
    assert 'T("fleet_no_goal")' in body
    assert "goal.Length == 0" in body


# ── the command file is shared, so writing to it must not eat anything ─────────────────────

def test_the_command_file_is_merged_rather_than_overwritten():
    """.fleet/commands.json is consumed whole and has more than one writer (EnqueueToFleet,
    the cockpit, task_router). A blind write would drop whatever is already queued."""
    body = _block(_code(), "bool AppendCommand(")
    assert "ReadAllText(cp" in body and "items.Add(item)" in body
    assert "UTF8Encoding(false)" in body, "a BOM here is read by Python on the other side"


# ── the conversation carries what it needs to be addressed ────────────────────────────────

def test_a_fleet_row_keeps_the_full_goal_and_the_conversation_ref():
    code = _code()
    body = _block(code, "void DiscoverTranscripts(", 4200)
    assert "Goal = goal" in body, "the goal text that identifies the conversation is discarded"
    assert '"sess:" + guid' in body, (
        "the conversation reference is not stored, so steer mode and the live snapshot refresh "
        "stay dark even once the relay records it")
    assert 'SS(gd, "guid")' in body, "the transcript's guid line is never read"


def test_the_ref_is_not_called_a_url():
    """A sess: reference is not navigable. /switch would try to open it as a page, which is how
    a resume silently becomes a fresh chat."""
    code = _code()
    body = _block(code, "void SendText(")
    fleet = body.index('target.Source == "fleet"')
    switch = body.index("/switch?url=")
    assert fleet < switch


def test_the_new_strings_exist_in_both_languages():
    code = _code()
    for k in ("fleet_steer_sent", "fleet_follow_sent", "fleet_follow_idle",
              "fleet_no_goal", "fleet_send_failed"):
        assert 'k == "%s"' % k in code, "missing string %r" % k


# ── and the deployed binary is not left behind the source ─────────────────────────────────

def test_the_running_binary_carries_this_change():
    """A source assertion cannot see a stale build. The chat window is a single compiled exe
    and the operator runs the exe, so a green suite against a binary that predates the fix
    would report a fix nobody has.
    """
    exe = os.path.join(UI, "CopilotChat.exe")
    if not os.path.isfile(exe):
        pytest.skip("no built binary here (CI)")
    with open(exe, "rb") as fh:
        blob = fh.read()
    needle = "fleet_follow_sent".encode("utf-16-le")
    assert needle in blob, (
        "CopilotChat.exe predates this change -- rebuild it with ui/build_and_run.bat, or the "
        "fleet conversation in the running window still cannot be answered")
