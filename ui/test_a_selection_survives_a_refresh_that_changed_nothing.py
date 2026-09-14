# -*- coding: utf-8 -*-
"""Selecting text in the main chat and pressing Ctrl+C copied nothing.

WHAT WAS ACTUALLY WRONG, and it was not the controls. Every message control in that window is
already selectable -- the user's own turn is a read-only TextBox, the settled answer is a
read-only RichTextBox with a selection brush and Focusable set. Nothing swallows Ctrl+C either:
the window's PreviewKeyDown handles Escape, Ctrl+B and the zoom keys, and no Copy command.

The selection was being destroyed. `CheckFleetSnapshot` fires when `.fleet/status.json`'s MTIME
changes, and a live fleet rewrites that file about once a second -- heartbeat, elapsed, the same
numbers -- whether or not anything a reader can see has moved. `RefreshFleetSnapshot` then ran
`_messages.Children.Clear()` and rebuilt every bubble from scratch. A selection cannot outlive
the control it lives in, so it was gone within the second, every second.

THE TRIGGER WAS THE DEFECT: a file moved, which is not the same fact as the screen would differ.
The rebuild is now guarded by a signature of the RENDERED CONTENT -- the transcript lines and the
status tail -- so a tick that changes nothing changes nothing.

NOT the worker dictionary, which carries per-second fields; signing that would have left the
guard in place and the rebuild running exactly as often, which is the shape of fix that looks
applied and is not.
"""
from __future__ import annotations

import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")


def _src():
    return io.open(CHAT, encoding="utf-8").read()


def _method(signature):
    src = _src()
    i = src.index(signature)
    j = src.find("\n    // ", i + 1)
    k = src.find("\n    void ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)] if ends else src[i:]


def _code_only(text):
    """Comments stripped. Asserting on raw text matches the comment that quotes the thing being
    forbidden -- the same trap hit three times in one day elsewhere in this repository."""
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


# ── the guard ─────────────────────────────────────────────────────────────────────────────

def test_the_fleet_view_does_not_rebuild_when_nothing_changed():
    """THE DEFECT, in one assertion."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "_fleetTurnSigs" in body, (
        "the fleet view rebuilds on every status.json write again; a text selection cannot "
        "survive one second of that")
    # PER TURN, NOT ONE STRING. The first version signed the whole transcript as a single value,
    # which can only answer "same or not" -- so a new turn still meant a full rebuild. A list
    # says WHERE it differs, which is what lets the new turns be appended instead.
    assert "List<string> _fleetTurnSigs" in _code_only(_src()), (
        "the signature is a single string again, so a growing transcript rebuilds the panel")


def test_nothing_is_cleared_before_the_comparison_is_made():
    """Clearing first and comparing afterwards would destroy the selection and then decide not
    to have destroyed it. Asserted as an ORDER over the panel, whatever the comparison looks
    like -- the first version pinned one `if` line and broke when the guard became a per-turn
    diff, reporting a failure about a defect that had just been fixed further."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert body.index("int common = 0;") < body.index("_messages.Children.Clear();"), (
        "the panel is cleared before the comparison runs")


def test_only_the_new_turns_are_added_when_the_past_is_unchanged():
    """THE HALF THE SIGNATURE GUARD DID NOT COVER. A tick with no change was already cheap; a
    tick that ADDS a turn still rebuilt everything, so a reader mid-selection lost it whenever
    the agent answered -- less often, which is not a property to rely on."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "for (int i = common; i < sigs.Count; i++)" in body, (
        "new turns are no longer appended; the panel is rebuilt when the transcript grows")
    i = body.index("for (int i = common; i < sigs.Count; i++)")
    assert "_messages.Children.Clear();" not in body[:i], (
        "the append path runs after a clear, which defeats it")


def test_a_past_that_changed_still_forces_a_rebuild():
    """Appending is only correct while the transcript is append-only. A different worker, a
    recycled conversation or a rewritten file means what is on screen is about something else,
    and keeping it would be worse than the flicker."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "common == _fleetTurnSigs.Count" in body, (
        "the append path no longer checks that everything already rendered still matches")


def test_the_signature_is_taken_from_what_is_displayed():
    """AND NOT FROM THE WORKER DICT. It carries fields that move every second -- elapsed, the
    heartbeat -- so signing it would keep the rebuild running at exactly the old rate while
    looking like a fix."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "txPre" in body, "the signature no longer derives from the transcript"
    i = body.index("var sigs = new List<string>();")
    arm = body[max(0, i - 500):i]
    assert "Serialize(w)" not in arm, (
        "the signature is being taken from the worker dictionary, which changes every second")
    # THE STATUS MUST NOT RE-ENTER THE SIGNATURE. If it did, every tick would differ, the
    # comparison would never hold and both the guard and the append would be decoration.
    # (The first draft of this assertion ended in `or True`, which is a test that cannot fail --
    # the green-by-omission this file exists to prevent, written into the file itself.)
    sig_block = body.split("var sigs = new List<string>();", 1)[1].split("int common", 1)[0]
    assert "tailPre" not in sig_block, (
        "the status tail is part of the per-turn signature again")
    assert "foreach (var m in txPre)" in sig_block, (
        "the signature is no longer built from the transcript turns")
    assert "ShowRunState(tailPre)" in body, "the status no longer reaches its own band"


def test_the_transcript_is_read_once():
    """The guard needs the transcript before it can decide, and the render needs it after. Two
    reads would put a second disk hit on a path that runs whenever the fleet writes."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert body.count("ReadTranscript(") == 1, (
        "the transcript is read %d times per refresh" % body.count("ReadTranscript("))


# ── and the controls were never the problem ───────────────────────────────────────────────

def test_the_answer_stays_selectable():
    """Pinned because the obvious wrong repair is to reach for the controls. They were correct
    the whole time, and a change here would be a change to the wrong thing."""
    src = _src()
    i = src.index("void RenderAssistantBody(")
    arm = src[i:i + 1600]
    assert "rtb.IsReadOnly = true;" in arm
    assert "rtb.Focusable = true;" in arm, "the answer can no longer take focus to be selected"
    assert "SelectionBrushProperty" in arm


def test_nothing_at_window_level_swallows_copy():
    """The other wrong repair: assuming a hotkey conflict. The window's PreviewKeyDown must not
    start handling Ctrl+C."""
    src = _src()
    i = src.index("PreviewKeyDown += delegate")
    arm = src[i:i + 1400]
    assert "Key.C " not in arm and "Key.C)" not in arm, (
        "the window now intercepts Ctrl+C")
