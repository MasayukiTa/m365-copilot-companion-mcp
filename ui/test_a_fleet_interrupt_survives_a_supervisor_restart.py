# -*- coding: utf-8 -*-
"""A fleet interrupt survives a supervisor restart, EXECUTED -- through the actual identity
decision, not a pre-filled fixture.

Context. Commit 3c93b59 made ui/CopilotChat.cs's OpenFromFleet and SyncRegistry fill a fleet
conversation's Goal and Transcript, falling back to the transcript's own first-line meta goal
(TranscriptMetaGoal) when no live worker dict is available. That logic was later pulled out of
ui/CopilotChat.cs into ui/FleetConvIdentity.cs, a WPF-free static class (ResolveGoal,
MergeForward, MergeBackfillOnly), so it can be compiled and run directly.

WHY THE PREVIOUS VERSION OF THIS FILE WAS REPLACED. It built each case's Conversation dict BY
HAND, with "goal" already set to a hard-coded string -- it never ran OpenFromFleet, SyncRegistry,
or (after the extraction) FleetConvIdentity at all. It proved DecideFleetSend correct given an
already-populated Conversation, which ui/test_the_chat_window_sends_what_was_typed.py's
"fleet_steer_live" case already covers; it could not have caught 3c93b59's actual bug (nothing
ever wrote the goal onto the object) because the fixture wrote it instead.

THIS version runs ui/FleetConvIdentity.cs's own decisions -- ResolveGoal / MergeForward /
MergeBackfillOnly -- through ui/harness/FleetConvIdentityHarness.cs, replaying OpenFromFleet's
and SyncRegistry's exact call sequences (see the harness source for the side-by-side comments),
and then feeds the RESULT into ui/ChatSend.cs's real LiveWorkerFor + DecideFleetSend to answer
the question that matters: is the interrupt delivered, or refused fleet_no_goal?

Four cases:

  * post_restart_meta_goal -- the supervisor-restart state that gave this file its name: no
    worker in status.json (workers: []), but the transcript's own first-line meta goal is
    available. ResolveGoal falls back to it, OpenFromFleet's MergeForward writes it onto the
    conversation, and the interrupt is delivered as a follow-up (add_goal), not refused.
  * live_worker -- a worker IS in status.json with its own goal, which differs from the
    transcript's meta goal. ResolveGoal must prefer the live goal (the freshest source), and
    LiveWorkerFor finds the worker, so the interrupt becomes a steer, not add_goal.
  * registry_only -- a row with no goal/transcript of its own yet (as DiscoverTranscripts would
    leave one before any registry poll) picks up both fields via SyncRegistry's
    MergeBackfillOnly path, with no live worker at all -- so a follow-up sent afterward is
    delivered rather than refused.
  * never_overwrite -- a row that ALREADY has a goal and transcript hits a transient miss (no
    live worker, no meta line, no fresh transcript resolved -- e.g. a slow disk read).
    MergeForward must leave the existing values in place rather than blanking them, so the
    interrupt is still delivered. This is the never-blank guard from
    ui/test_a_fleet_conversation_opened_from_the_cockpit_keeps_its_identity.py, now proven at
    runtime instead of only read as text.

Skips only on a non-Windows host (the code under test is C# built by the .NET Framework csc).
On Windows a missing csc skips unless REQUIRE_CSC=1 (which CI sets), and then it fails.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import childproc  # noqa: E402

UI = os.path.join(REPO, "ui")
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
CSC = os.path.join(FW, "csc.exe")

#: The shipped identity-merge decision + the shipped send decision + the test-only harness that
#: replays OpenFromFleet's/SyncRegistry's own call sequences against them.
SOURCES = [
    os.path.join(UI, "FleetConvIdentity.cs"),
    os.path.join(UI, "ChatSend.cs"),
    os.path.join(UI, "harness", "FleetConvIdentityHarness.cs"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the code under test is C# compiled by the .NET Framework csc, which exists only "
           "on Windows")

TRANSCRIPT = r"C:\repo\.fleet\transcripts\1799990000_w0.jsonl"
META_GOAL = "investigate the copper-foil yield drop and report"
LIVE_GOAL = "steer this: focus on the west-line sensors only"
REG_GOAL = "audit the disk cleanup job for stray temp files"
INTERRUPT = "and what about last week's numbers?"

#: status.json right after a supervisor restart: the fleet process is up (running=true) but has
#: not re-registered any worker yet.
RESTART_STATUS = json.dumps({"running": True, "open_tabs": 0, "max_concurrent": 6, "workers": []})

#: status.json with exactly one live worker, matching TRANSCRIPT.
LIVE_STATUS = json.dumps({
    "running": True, "open_tabs": 1, "max_concurrent": 6,
    "workers": [{"name": "w0", "transcript": TRANSCRIPT, "status": "running"}],
})

CASES = [
    {
        "id": "post_restart_meta_goal",
        "op": "open",
        "live_goal": "",                       # no worker in status.json
        "transcript_meta_goal": META_GOAL,      # but the transcript's first line has one
        "existing_source": "", "existing_name": "", "existing_transcript": "", "existing_goal": "",
        "worker": "w0", "transcript_path": TRANSCRIPT, "conv_url": "sess:aaaabbbb-cccc-dddd-eeee-ffff00001111",
        "status_text": RESTART_STATUS, "text": INTERRUPT, "lang": 1,
    },
    {
        "id": "live_worker",
        "op": "open",
        "live_goal": LIVE_GOAL,                 # a live worker dict with its OWN goal
        "transcript_meta_goal": META_GOAL,      # different text -- live must win
        "existing_source": "", "existing_name": "", "existing_transcript": "", "existing_goal": "",
        "worker": "w0", "transcript_path": TRANSCRIPT, "conv_url": "sess:11112222-3333-4444-5555-666677778888",
        "status_text": LIVE_STATUS, "text": INTERRUPT, "lang": 1,
    },
    {
        "id": "registry_only",
        "op": "registry_backfill",
        "existing_source": "fleet", "existing_name": "w0", "existing_transcript": "", "existing_goal": "",
        "reg_goal": REG_GOAL, "reg_transcript": TRANSCRIPT,
        "conv_url": "sess:99990000-1111-2222-3333-444455556666",
        "status_text": RESTART_STATUS, "text": INTERRUPT, "lang": 1,
    },
    {
        "id": "never_overwrite",
        "op": "open",
        "live_goal": "", "transcript_meta_goal": "",   # transient miss: nothing new resolved
        "existing_source": "fleet", "existing_name": "w0",
        "existing_transcript": TRANSCRIPT, "existing_goal": META_GOAL,   # already had both
        "worker": "w0", "transcript_path": "",          # a fresh path was NOT found this time
        "conv_url": "sess:aabbccdd-eeff-0011-2233-445566778899",
        "status_text": RESTART_STATUS, "text": INTERRUPT, "lang": 1,
    },
]


def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("fleet_conv_identity")
    exe = str(d / "FleetConvIdentityHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + exe,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(exe), (
        "csc could not build FleetConvIdentity.cs + ChatSend.cs with the harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))

    cpath = str(d / "cases.json")
    rpath = str(d / "results.json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(CASES, fh, ensure_ascii=False)
    run = childproc.run([exe, "run", cpath, rpath], timeout=180)
    assert run.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        run.returncode, run.stdout, run.stderr)
    with open(rpath, encoding="utf-8") as fh:
        got = json.load(fh)
    return got


def test_all_cases_ran_without_error(results):
    assert set(results) == {c["id"] for c in CASES}, sorted(results)
    for cid, r in results.items():
        assert not r["error"], (cid, r["error"])


def test_post_restart_resolves_the_goal_from_the_transcript_meta_line(results):
    """No worker in status.json -> ResolveGoal falls back to the transcript's own first-line
    goal, and OpenFromFleet's MergeForward writes it onto the conversation."""
    r = results["post_restart_meta_goal"]
    assert r["resolved_goal"] == META_GOAL
    assert r["goal"] == META_GOAL
    assert r["transcript"] == TRANSCRIPT
    assert r["source"] == "fleet"
    assert r["live"] == "", "no worker in status.json -> nothing to steer"


def test_post_restart_interrupt_is_delivered_not_refused(results):
    """This is the regression 3c93b59 fixed, run for real: the restart-time interrupt must NOT
    be the fleet_no_goal refusal the operator saw before that fix."""
    fs = results["post_restart_meta_goal"]["fleet_send"]
    assert fs["kind"] == "command", fs
    assert fs["key"] == "add_goal", fs
    assert fs["refusal_key"] is None, fs
    assert fs["item"]["follow_up_to"] == META_GOAL, fs
    assert fs["item"]["resume_conv"] == "sess:aaaabbbb-cccc-dddd-eeee-ffff00001111", fs


def test_live_worker_goal_wins_over_the_transcript_meta_line(results):
    """ResolveGoal's priority: a live status.json worker dict beats the transcript's own
    first-line goal when both are available (the live source is the freshest)."""
    r = results["live_worker"]
    assert r["resolved_goal"] == LIVE_GOAL
    assert r["goal"] == LIVE_GOAL
    assert r["live"] == "w0"


def test_live_worker_interrupt_is_a_steer_not_a_follow_up(results):
    fs = results["live_worker"]["fleet_send"]
    assert fs["kind"] == "command", fs
    assert fs["key"] == "steer", fs
    assert fs["item"] == {"worker": "w0", "text": INTERRUPT}, fs


def test_registry_only_backfills_goal_and_transcript_with_no_live_worker(results):
    """SyncRegistry's path: a row with neither field yet picks up both from the registry poll,
    with no OpenFromFleet click and no live worker at all."""
    r = results["registry_only"]
    assert r["goal"] == REG_GOAL
    assert r["transcript"] == TRANSCRIPT
    assert r["live"] == ""
    fs = r["fleet_send"]
    assert fs["kind"] == "command" and fs["key"] == "add_goal", fs
    assert fs["item"]["follow_up_to"] == REG_GOAL, fs


def test_a_transient_miss_never_blanks_an_identity_the_row_already_had(results):
    """The never-overwrite guard, proven at runtime: a row that already has a goal/transcript
    keeps them when this read comes back empty (no live worker, no meta line, no fresh
    transcript path), and the interrupt is still delivered rather than refused."""
    r = results["never_overwrite"]
    assert r["resolved_goal"] == "", "this case's whole point is a fresh read that found nothing"
    assert r["goal"] == META_GOAL, "MergeForward blanked a goal the row already had"
    assert r["transcript"] == TRANSCRIPT, "MergeForward blanked a transcript the row already had"
    fs = r["fleet_send"]
    assert fs["kind"] == "command" and fs["key"] == "add_goal", (
        "a blanked identity would have been refused fleet_no_goal here", fs)
