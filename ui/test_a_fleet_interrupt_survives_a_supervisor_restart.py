# -*- coding: utf-8 -*-
"""A fleet interrupt survives a supervisor restart, EXECUTED.

Context. Commit 3c93b59 made ui/CopilotChat.cs's OpenFromFleet and SyncRegistry fill a fleet
conversation's Goal and Transcript, falling back to the transcript's own first-line meta goal
(TranscriptMetaGoal) when no live worker dict is available. The one state that had no execution
test was the RESTART-DIRECTLY-AFTER one: the supervisor is rebuilding .fleet/status.json, so
that file exists but names no worker for this conversation yet, while the transcript on disk --
and therefore the goal carried onto the Conversation object -- is still there.

What is checked here, by RUNNING the shipped send path (ui/ChatSend.cs) the way
ui/test_the_chat_window_sends_what_was_typed.py does -- compiled with the real csc against the
same RecordingWorld harness (ui/testdata/ChatSendHarness.cs) and the fleet's own command
writer (ui/FleetCommands.cs):

  * With the goal intact, an interrupt typed into a fleet conversation whose worker is absent
    from status.json (workers empty; the fleet process is up, running=true) is NOT refused with
    fleet_no_goal. It becomes a follow-up add_goal that carries resume_conv (the conversation's
    sess:<guid>) and follow_up_to (the full goal), exactly as the no-live-worker branch of
    SendToFleetConversation prescribes -- the message reaches the fleet rather than being lost.
  * The ONLY thing that decides refuse-vs-deliver here is whether the goal was carried. The
    same status.json and transcript, with an EMPTY goal on the Conversation (the pre-3c93b59
    state, where nothing filled it), is refused fleet_no_goal. So the goal being preserved is
    load-bearing, and this test would go green on the broken build only if the goal were blank
    -- which is the very regression 3c93b59 fixed.
  * A worker absent from status.json is not steered (there is no live worker to steer); the
    interrupt is a follow-up, not a steer command.

This asserts on ChatSend's DELIVERY decision given a Conversation that already carries its
goal. It does not re-test that OpenFromFleet/SyncRegistry populate that goal -- that is the
source-level job of ui/test_a_fleet_conversation_opened_from_the_cockpit_keeps_its_identity.py.
The two together cover the round trip: the goal is filled there, and here it is shown to be
what keeps a restart-time interrupt from being refused.

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

#: The shipped send path + the shipped command writer + the test-only harness. The oracle
#: (ChatDecisionsOriginal.cs) is compiled in too because ChatSendHarness.cs's Main runs both
#: sides; this test only reads the "extracted" (shipped) side.
SOURCES = [
    os.path.join(UI, "ChatSend.cs"),
    os.path.join(UI, "FleetCommands.cs"),
    os.path.join(UI, "testdata", "ChatDecisionsOriginal.cs"),
    os.path.join(UI, "testdata", "ChatSendHarness.cs"),
]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="the code under test is C# compiled by the .NET Framework csc, which exists only "
           "on Windows")

# A durable conversation id (sess:<guid>) and the on-disk transcript that survives the restart.
SESS = "sess:aaaabbbb-cccc-dddd-eeee-ffff00001111"
TRANSCRIPT = r"C:\repo\.fleet\transcripts\1799990000_w0.jsonl"
GOAL = "  investigate the copper-foil yield drop and report  "
GOAL_TRIMMED = "investigate the copper-foil yield drop and report"
INTERRUPT = "and what about last week's numbers?"

#: status.json as the supervisor leaves it in the instant right after a restart: the fleet
#: process is up (running=true) but it has not re-registered any worker yet, so "workers" is
#: empty. LiveWorkerFor finds no worker for this transcript -> no live worker -> the interrupt
#: takes the follow-up branch, not the steer branch. running=true means FleetState()[0]==1, so
#: a delivered follow-up is acknowledged with fleet_follow_sent.
RESTART_STATUS = {"running": True, "open_tabs": 0, "max_concurrent": 6, "workers": []}


def _require_csc():
    return os.environ.get("REQUIRE_CSC", "").strip() == "1"


def _fleet_conv(goal):
    """A fleet conversation as OpenFromFleet/SyncRegistry hand it to ChatSend: source fleet,
    the durable sess id, the surviving transcript path, and the goal carried onto it."""
    return {
        "id": "f1", "source": "fleet", "conv_url": SESS, "name": "w0",
        "transcript": TRANSCRIPT, "goal": goal, "title": "investigate the copper\u2026",
        "messages": [["U", "investigate the copper-foil yield drop and report"], ["A", "done"]],
    }


CASES = [
    # goal intact -> the interrupt is delivered as a follow-up, not refused
    {"id": "goal_kept", "input": INTERRUPT, "conv": _fleet_conv(GOAL), "lang": 1},
    # goal blank (the pre-3c93b59 restart state) -> refused fleet_no_goal, same status/transcript
    {"id": "goal_blank", "input": INTERRUPT, "conv": _fleet_conv(""), "lang": 1},
]


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if not os.path.isfile(CSC):
        msg = "csc.exe is not at %s" % CSC
        if _require_csc():
            pytest.fail(msg + " and REQUIRE_CSC=1")
        pytest.skip(msg)
    d = tmp_path_factory.mktemp("fleet_restart")
    exe = str(d / "ChatSendHarness.exe")
    r = childproc.run([CSC, "/nologo", "/target:exe", "/out:" + exe,
                       "/r:" + os.path.join(FW, "System.Web.Extensions.dll")] + SOURCES,
                      timeout=300)
    assert r.returncode == 0 and os.path.isfile(exe), (
        "csc could not build the send path with its harness (rc=%s):\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))

    root = str(d / "run")
    os.makedirs(root)
    # One status.json, shared by every case: the restart-time file with no worker rows.
    status_dir = os.path.join(root, "status")
    os.makedirs(status_dir)
    status_path = os.path.join(status_dir, "status.json")
    with open(status_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(RESTART_STATUS, fh)

    cases = []
    for k in CASES:
        k = dict(k)
        k["status_path"] = status_path
        cases.append(k)
    cpath = os.path.join(root, "cases.json")
    rpath = os.path.join(root, "results.json")
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False)
    work = os.path.join(root, "commands")
    run = childproc.run([exe, cpath, rpath, work], timeout=180)
    assert run.returncode == 0, "the harness failed (rc=%s):\n%s\n%s" % (
        run.returncode, run.stdout, run.stderr)
    with open(rpath, encoding="utf-8") as fh:
        got = json.load(fh)
    return got


def _extracted(results, cid):
    return results[cid]["extracted"]


def _payloads(out, key=None):
    found = []
    for line in out["trace"]:
        if line.startswith("AppendCommand|"):
            _, k, js = line.split("|", 2)
            if key is None or k == key:
                found.append((k, json.loads(js)))
    return found


def _said(out):
    return [ln[len("AddAssistant|"):] for ln in out["trace"] if ln.startswith("AddAssistant|")]


def test_both_cases_ran(results):
    assert set(results) == {"goal_kept", "goal_blank"}, sorted(results)
    for cid, r in results.items():
        assert not r["extracted"]["error"], (cid, r["extracted"]["error"])


def test_no_live_worker_when_status_json_names_none(results):
    """The restart-time status.json is up (running=true) but lists no worker, so the
    conversation's transcript matches nothing: there is no live worker to steer."""
    for cid in ("goal_kept", "goal_blank"):
        assert _extracted(results, cid)["decisions"]["live_worker"] == "", cid


def test_the_interrupt_is_delivered_as_a_follow_up_not_refused(results):
    """With the goal carried, the restart-time interrupt is NOT the fleet_no_goal refusal the
    operator saw before 3c93b59. It is a follow-up add_goal that keeps the conversation: it
    carries resume_conv (the durable sess id) and follow_up_to (the full goal), and because the
    fleet is up it is acknowledged fleet_follow_sent."""
    out = _extracted(results, "goal_kept")
    payloads = _payloads(out)
    assert payloads == [("add_goal", {
        "text": "[follow-up from the user] " + INTERRUPT
                + "\nAnswer only this follow-up, building on the work so far. "
                  "Do not start over. Write DONE when finished, or FAIL and why.",
        "resume_conv": SESS,
        "follow_up_to": GOAL_TRIMMED,
        "priority": True,
    })], out["trace"]
    assert _said(out) == ["T:fleet_follow_sent"], out["trace"]
    assert "AddAssistant|T:fleet_no_goal" not in out["trace"], (
        "the interrupt was refused as if the conversation had no goal -- the restart-time "
        "fallback that carries the goal is what this guards")
    # it is a follow-up, not a steer: no live worker was addressed
    assert not _payloads(out, "steer"), out["trace"]


def test_a_blank_goal_is_the_only_thing_that_brings_the_refusal_back(results):
    """Same status.json, same transcript, same interrupt -- only the goal removed. Now it IS
    refused fleet_no_goal and nothing is written. This is the state 3c93b59 fixed: if the goal
    is not carried onto the conversation at restart time, the interrupt is lost to the refusal.
    Proving the refusal returns exactly when the goal is blank shows the goal is the deciding
    factor above, not some incidental property of the fixture."""
    out = _extracted(results, "goal_blank")
    assert _said(out) == ["T:fleet_no_goal"], out["trace"]
    assert not _payloads(out), "a conversation with no goal still wrote a fleet command"
