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

The defect was upstream of ChatSend.cs entirely: nothing that built a fleet Conversation object
in ui/CopilotChat.cs ever copied Goal or Transcript onto it. Fixed by commit 3c93b59
(OpenFromFleet / SyncRegistry) and later pulled out of ui/CopilotChat.cs into
ui/FleetConvIdentity.cs, a WPF-free static class, so the merge rules themselves -- not just their
call sites -- can be run by a test (ui/test_a_fleet_interrupt_survives_a_supervisor_restart.py
compiles and executes ui/FleetConvIdentity.cs + ui/ChatSend.cs directly, via
ui/harness/FleetConvIdentityHarness.cs; that is the RUNTIME half of this coverage). THIS file
stays source-text: it checks that ui/CopilotChat.cs actually WIRES UP the extracted decisions at
both call sites, which a runtime test of FleetConvIdentity.cs alone cannot see (it could pass
while OpenFromFleet or SyncRegistry called nothing, or called the wrong method).

  * OpenFromFleet() -- the method .fleet/open.json drives when a cockpit card is clicked
    (CheckOpenRequest) -- resolves the live worker dict (`wkr`) and the on-disk transcript path
    (`transcriptPath`) itself, then hands them to FleetConvIdentity.ResolveGoal / MergeForward /
    MergeBackfillOnly to decide what lands on the `Conversation c` object it hands to `_conv` --
    the SAME object ChatSend.SendText later reads `.Goal` and `.Transcript` from.
  * SyncRegistry() -- the poll of `.fleet/conversations.json`, the ONLY feed that updates a
    fleet conversation already in the sidebar while the window stays open (DiscoverTranscripts
    scans the transcripts directory once, at startup) -- reads a "goal" field the registry now
    writes (relay/fleet_runner.py's `_register_convs`) and backfills it via
    FleetConvIdentity.MergeBackfillOnly, same never-overwrite rule as OpenFromFleet's Source/Name.
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
    transcript's own first-line "goal" as the fallback (survives a restarted fleet). The
    priority itself is FleetConvIdentity.ResolveGoal's job (see
    ui/test_a_fleet_conversation_identity_merge_runs.py) -- this only checks the call site
    passes it the right two values."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert 'string liveGoal = wkr != null ? SS(wkr, "goal") : ""' in body
    assert "TranscriptMetaGoal(transcriptPath)" in body
    assert "FleetConvIdentity.ResolveGoal(liveGoal, TranscriptMetaGoal(transcriptPath))" in body
    assert "bestGoal" in body


def test_open_from_fleet_copies_goal_and_transcript_onto_the_conversation():
    """The Conversation object built/reused here is the SAME one ChatSend.SendText later reads
    .Goal and .Transcript from (it becomes `_conv`). Without this assignment the resolution
    above is dead: it renders the body correctly and still leaves the object that matters
    empty. The forward-merge rule itself (fresh non-empty value wins) is
    FleetConvIdentity.MergeForward's job -- checked at runtime, not here."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert "c.Transcript = FleetConvIdentity.MergeForward(c.Transcript, transcriptPath)" in body, (
        "the transcript path is computed and never stored on c -- LiveWorkerFor "
        "(ChatSend.cs) requires c.Transcript non-empty and will always answer "
        '"" (no live worker), so a mid-run interrupt cannot be steered')
    assert "c.Goal = FleetConvIdentity.MergeForward(c.Goal, bestGoal)" in body, (
        "the resolved goal is never stored on c -- DecideFleetSend (ChatSend.cs) refuses "
        "fleet_no_goal whenever c.Goal is empty, regardless of whether a real goal was "
        "available")


def test_open_from_fleet_claims_source_fleet_only_for_an_unclaimed_row():
    """DecideDoor (ChatSend.cs) routes a send through the fleet channel only when
    c.Source == "fleet". A stub Conversation created here defaults to Source == "" and must be
    claimed, or the fleet channel is unreachable from this open path at all -- but a row already
    carrying a different, real source (e.g. a plain Copilot-side orphan sharing this exact
    ConvUrl, reached through OpenConversation's "haven't loaded yet" fallback) must not be
    silently reclassified as a fleet conversation. Backfill-only (claim iff currently empty) is
    FleetConvIdentity.MergeBackfillOnly's job -- checked at runtime, not here."""
    code = _code()
    body = _block(code, "void OpenFromFleet(string url, string worker, string transcriptHint)", 6500)
    assert 'c.Source = FleetConvIdentity.MergeBackfillOnly(c.Source, "fleet")' in body
    assert "c.Name = FleetConvIdentity.MergeBackfillOnly(c.Name, worker)" in body


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


def test_sync_registry_backfills_goal_via_the_extracted_merge_rule():
    """The registry poll runs continuously; a row it already added (in an earlier poll, or by
    DiscoverTranscripts at startup) must pick up a goal that only became available later, but a
    goal it already has must never be replaced -- the same rule merge_conv_rows enforces on the
    relay side (relay/test_conv_registry_points_at_this_run.py), and the same rule
    FleetConvIdentity.MergeBackfillOnly implements (checked at runtime, not here -- this only
    checks the call site routes through it rather than re-inlining the guard)."""
    code = _code()
    body = _block(code, "void SyncRegistry()")
    assert "existingC.Goal = FleetConvIdentity.MergeBackfillOnly(existingC.Goal, regGoal)" in body
    assert "existingC.Transcript = FleetConvIdentity.MergeBackfillOnly(existingC.Transcript, transcript)" in body


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
    needle2 = "FleetConvIdentity".encode("utf-8")
    assert needle2 in blob, (
        "CopilotChat.exe predates the FleetConvIdentity extraction -- rebuild it")
