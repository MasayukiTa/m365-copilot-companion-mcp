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
    assert "_fleetRenderSig" in body, (
        "the fleet view rebuilds on every status.json write again; a text selection cannot "
        "survive one second of that")
    assert "if (sig == _fleetRenderSig) return;" in body, (
        "the signature is computed and not acted on")


def test_the_rebuild_happens_after_the_guard_and_not_before():
    """Clearing first and comparing afterwards would destroy the selection and then decide not
    to have destroyed it."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert body.index("if (sig == _fleetRenderSig) return;") < body.index(
        "_messages.Children.Clear();"), (
        "the panel is cleared before the guard runs")


def test_the_signature_is_taken_from_what_is_displayed():
    """AND NOT FROM THE WORKER DICT. It carries fields that move every second -- elapsed, the
    heartbeat -- so signing it would keep the rebuild running at exactly the old rate while
    looking like a fix."""
    body = _code_only(_method("void RefreshFleetSnapshot()"))
    assert "txPre" in body and "tailPre" in body, (
        "the signature no longer derives from the transcript and the status tail")
    i = body.index("string sig =")
    arm = body[max(0, i - 500):i]
    assert "Serialize(w)" not in arm, (
        "the signature is being taken from the worker dictionary, which changes every second")


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
