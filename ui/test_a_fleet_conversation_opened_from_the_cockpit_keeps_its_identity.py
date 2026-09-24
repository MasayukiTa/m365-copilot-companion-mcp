# -*- coding: utf-8 -*-
"""A fleet conversation opened from the cockpit (or a mid-run registry poll) must arrive with
its identity intact, not just its messages.

MEASURED 2026-09-24, reported by the operator twice in one session, with a screenshot each
time:

  1. A finished fleet conversation ("状態: done, ターン 1/40, 理由: refuter#1: UPHELD"), opened
     from the chat window, refused a follow-up:
         この会話を識別するゴール本文が記録されていないため、続きを投入できません。
  2. An INTERRUPT typed into the SAME window while the worker was still RUNNING got the
     identical refusal -- "走行中のにわりこみをメインから行ったらこうなった。これ網羅してないよ
     ね".

ChatSend.cs was innocent both times: DecideFleetSend refuses fleet_no_goal only when
`c.Goal` is empty (ui/ChatSend.cs, DecideFleetSend), and LiveWorkerFor only ever finds a live
worker when `c.Transcript` is non-empty AND matches the CURRENT status.json row for that worker
(ui/ChatSend.cs, LiveWorkerFor) -- see ui/test_the_chat_window_sends_what_was_typed.py's
"fleet_steer_live" case, which proves that logic correct given a Conversation whose Goal and
Transcript are already populated.

The defect was upstream of ChatSend.cs entirely: nothing that builds a fleet Conversation
object in ui/CopilotChat.cs ever copied Goal or Transcript onto it.

  * OpenFromFleet() -- the method .fleet/open.json drives when a cockpit card is clicked
    (CheckOpenRequest) -- resolves the live worker dict (`wkr`) and the on-disk transcript path
    (`transcriptPath`) itself, using both to render the conversation body, but never assigned
    either onto the `Conversation c` object it hands to `_conv` -- the SAME object
    ChatSend.SendText later reads `.Goal` and `.Transcript` from. A brand-new stub got neither
    field, ever; a reused row (matched by ConvUrl) never had a missing field filled in even
    when the live worker dict it just read had the answer in hand.
  * SyncRegistry() -- the poll of `.fleet/conversations.json`, the ONLY feed that updates a
    fleet conversation already in the sidebar while the window stays open (DiscoverTranscripts
    scans the transcripts directory once, at startup) -- built its Conversation rows from
    "url"/"title"/"source"/"transcript"/"name"/"ts" and never read a "goal" field, because the
    registry never wrote one either (relay/fleet_runner.py's _register_convs carried only a
    truncated, display-only `title`, made from the goal but not the goal itself).

So a fleet conversation reached through EITHER of the window's two "open" paths carried a
permanently empty Goal, and often an empty Transcript too -- refusing a follow-up or a
mid-run interrupt identically, regardless of whether the worker had finished or was still on
its current turn.

Fixed by carrying the goal the rest of the way:
  * relay/fleet_runner.py's `_register_convs` now writes `"goal": w.goal` into every registry
    row, and `merge_conv_rows` backfills a missing "goal" onto an existing row without ever
    overwriting one already recorded (relay/test_conv_registry_points_at_this_run.py).
  * ui/CopilotChat.cs's SyncRegistry() reads that field and sets `c.Goal` -- on a fresh row and,
    as a backfill, on one it finds already registered.
  * ui/CopilotChat.cs's OpenFromFleet() resolves the best available goal (the live worker
    dict's own "goal", falling back to the transcript's own first-line "goal" via the new
    TranscriptMetaGoal() helper) and copies it, plus the transcript path it already computed,
    onto `c` -- again without ever blanking a value the row already had.
"""
from __future__ import annotations

import os
import re

import pytest

UI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(UI, "CopilotChat.cs")


def _code() -> str:
    with open(SRC, encoding="utf-8-sig") as fh:
        text = fh.read()
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _block(code: str, start: str, length: int = 3200) -> str:
    i = code.index(start)
    return code[i:i + length]


# ── OpenFromFleet: the cockpit "open" / retry path ──────────────────────────────────────────

def test_open_from_fleet_resolves_a_goal_from_the_live_worker_or_the_transcript():
    """Both sources OpenFromFleet already has in hand at that point: the live status.json
    worker dict first (survives a finished worker whose slot has not been reused), the
    transcript's own first-line "goal" as the fallback (survives a restarted fleet)."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert 'string liveGoal = wkr != null ? SS(wkr, "goal") : ""' in body
    assert "TranscriptMetaGoal(transcriptPath)" in body
    assert "bestGoal" in body


def test_open_from_fleet_copies_goal_and_transcript_onto_the_conversation():
    """The Conversation object built/reused here is the SAME one ChatSend.SendText later reads
    .Goal and .Transcript from (it becomes `_conv`). Without this assignment the resolution
    above is dead: it renders the body correctly and still leaves the object that matters
    empty."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert "c.Transcript = transcriptPath" in body, (
        "the transcript path is computed and never stored on c -- LiveWorkerFor "
        "(ChatSend.cs) requires c.Transcript non-empty and will always answer "
        '"" (no live worker), so a mid-run interrupt cannot be steered')
    assert "c.Goal = bestGoal" in body, (
        "the resolved goal is never stored on c -- DecideFleetSend (ChatSend.cs) refuses "
        "fleet_no_goal whenever c.Goal is empty, regardless of whether a real goal was "
        "available")


def test_open_from_fleet_never_blanks_an_identity_the_row_already_had():
    """A reused row (found by matching ConvUrl) may already carry a correct Goal/Transcript
    from DiscoverTranscripts or SyncRegistry; a transient miss here (no wkr, no meta line, e.g.
    a slow disk read) must not stomp it back to empty."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert 'if (!string.IsNullOrEmpty(transcriptPath)) c.Transcript = transcriptPath;' in body
    assert 'if (!string.IsNullOrEmpty(bestGoal)) c.Goal = bestGoal;' in body


def test_open_from_fleet_claims_source_fleet_only_for_an_unclaimed_row():
    """DecideDoor (ChatSend.cs) routes a send through the fleet channel only when
    c.Source == "fleet". A stub Conversation created here defaults to Source == "" and must be
    claimed, or the fleet channel is unreachable from this open path at all -- but a row already
    carrying a different, real source (e.g. a plain Copilot-side orphan sharing this exact
    ConvUrl, reached through OpenConversation's "haven't loaded yet" fallback) must not be
    silently reclassified as a fleet conversation."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert 'if (string.IsNullOrEmpty(c.Source)) c.Source = "fleet";' in body


def test_transcript_meta_goal_helper_reads_only_the_first_line():
    """The meta line -- {"meta": true, "key", "name", "goal", "ts"} -- is written once, at
    _Transcript construction (relay/relay_fleet.py), and never rewritten; reading past it would
    just be slower for the same answer, and TryParseTranscriptLine already treats a role-less
    line as nothing to show, so a naive full-transcript scan would silently return nothing."""
    code = _code()
    body = _block(code, "string TranscriptMetaGoal(string path)", 900)
    assert "sr.ReadLine()" in body
    assert 'SS(meta, "goal")' in body


# ── SyncRegistry: the poll that updates while the window is already open ───────────────────

def test_sync_registry_reads_the_goal_field():
    code = _code()
    body = _block(code, "void SyncRegistry()")
    assert 'string regGoal = SS(d, "goal")' in body


def test_sync_registry_sets_goal_on_a_freshly_discovered_row():
    code = _code()
    body = _block(code, "void SyncRegistry()")
    assert "c.Goal = regGoal" in body


def test_sync_registry_backfills_goal_on_a_row_it_already_knows_without_overwriting():
    """The registry poll runs continuously; a row it already added (in an earlier poll, or by
    DiscoverTranscripts at startup) must pick up a goal that only became available later, but a
    goal it already has must never be replaced -- the same rule merge_conv_rows enforces on the
    relay side (relay/test_conv_registry_points_at_this_run.py)."""
    code = _code()
    body = _block(code, "void SyncRegistry()")
    assert "existingC.Goal = regGoal" in body
    backfill = _block(body, "if (existingC != null)", 500)
    assert "string.IsNullOrEmpty(existingC.Goal)" in backfill, (
        "the backfill is not conditioned on the existing goal being empty -- it would "
        "overwrite a goal the row already has")


def test_the_running_binary_carries_this_change():
    """Same guard as ui/test_a_fleet_conversation_can_be_answered.py: a source assertion cannot
    see a stale build, and the operator runs the compiled exe."""
    exe = os.path.join(UI, "CopilotChat.exe")
    if not os.path.isfile(exe):
        pytest.skip("no built binary here (CI)")
    with open(exe, "rb") as fh:
        blob = fh.read()
    # A METHOD NAME IS METADATA, NOT A STRING LITERAL. .NET keeps identifiers in the #Strings
    # heap as UTF-8; only string literals live in #US as UTF-16. The first version searched for
    # UTF-16 and failed against a binary that did carry the method -- measured 2026-09-24.
    needle = "TranscriptMetaGoal".encode("utf-8")
    assert needle in blob, (
        "CopilotChat.exe predates this change -- rebuild it, or a fleet conversation opened "
        "from the cockpit still carries no goal")
