# -*- coding: utf-8 -*-
"""Choosing an entry on a card and being dropped at the end of the conversation.

THE OTHER HALF of taking the body out of the worker card. The card now lists what happened and
holds no reading surface; the full text opens in the chat window. If that window opens at the
END, the reader has to find again the entry they had just chosen -- the review asked about the
restructure said it in one line: 「単に会話の末尾を開く設計では、対象を探し直す負担が残る」.

SO THE POSITION TRAVELS. The cockpit writes `turn_index` into `.fleet/open.json` beside the
worker and url it already wrote; the chat window reads it, brings that entry into view and marks
it briefly.

AND FOLLOWING THE TAIL IS SUSPENDED. Arriving at turn 4 of 40 and then being dragged to the
bottom by the next status write is the same defect with an extra step. The scroll handler
re-arms following by itself when the reader returns to the bottom, so this only has to stop
claiming they are there.

WHAT IS DELIBERATE ABOUT THE FAILURE MODE: an index that does not land inside the panel is
IGNORED, not clamped. A silent jump to the wrong entry is worse than no jump, because it reads
as the right one.
"""
from __future__ import annotations

import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")
CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")


def _code(path):
    return "\n".join(ln for ln in io.open(path, encoding="utf-8").read().splitlines()
                     if not ln.strip().startswith("//"))


def _method(path, signature, stop="\n    // "):
    src = io.open(path, encoding="utf-8").read()
    i = src.index(signature)
    j = src.find(stop, i + 1)
    return src[i:j] if j > 0 else src[i:]


# ── the cockpit sends the position ────────────────────────────────────────────────────────

def test_the_card_entry_names_the_entry_it_opens():
    body = _code(COCKPIT)
    assert "OpenWorker(wn, cu, idx)" in body, (
        "a card entry no longer opens at its own position")
    assert "int idx = firstIndex + shown;" in body, (
        "the index is not computed over the whole transcript, so it names the n-th of the "
        "three shown rather than the entry the reader chose")


def test_the_index_is_over_the_whole_transcript():
    """The card lists the LAST few turns. Sending 0..2 would open the first three entries of the
    conversation, which is a different conversation from the one on the card."""
    body = _code(COCKPIT)
    assert "int firstIndex = Math.Max(0, total - turns.Count);" in body


def test_no_position_asked_for_is_distinguishable_from_position_zero():
    """The card header and the history list still open a conversation without choosing an entry.
    Writing 0 for both would send every one of those to the first turn."""
    body = _method(COCKPIT, "void OpenWorker(string name, string url, int turnIndex)")
    assert 'if (turnIndex >= 0) o["turn_index"] = turnIndex;' in body, (
        "turn_index is written unconditionally, so 'just open it' became 'open at turn 0'")
    assert "void OpenWorker(string name, string url) { OpenWorker(name, url, -1); }" in _code(COCKPIT)


# ── the chat window lands on it ───────────────────────────────────────────────────────────

def test_the_chat_window_reads_the_position():
    body = _code(CHAT)
    assert 'd.ContainsKey("turn_index")' in body, "the chat window ignores the chosen entry"
    assert "ScrollToTurn(wantIdx)" in body


def test_arriving_stops_the_view_following_the_tail():
    """Otherwise the next status write drags the reader to the bottom and the landing is undone
    before they have read a line."""
    body = _code(CHAT)
    i = body.index("void ScrollToTurn(int index)")
    arm = body[i:i + 1400]
    assert "_stickBottom = false;" in arm, (
        "the view keeps following the tail after jumping to an entry")
    assert "BringIntoView()" in arm


def test_an_index_outside_the_panel_is_ignored_rather_than_clamped():
    """A silent jump to the wrong entry reads as the right one."""
    body = _code(CHAT)
    i = body.index("void ScrollToTurn(int index)")
    arm = body[i:i + 1400]
    assert "at >= _messages.Children.Count) return;" in arm, (
        "an out-of-range index is being clamped or forced instead of ignored")
    assert "index < 0" in arm


def test_the_entry_is_marked_when_it_is_reached():
    """Landing without a mark leaves the reader to work out which of several similar blocks they
    were sent to."""
    body = _code(CHAT)
    assert "HighlightBriefly(target)" in body
    i = body.index("void HighlightBriefly(FrameworkElement target)")
    arm = body[i:i + 1200]
    assert "DispatcherTimer" in arm and "Stop();" in arm, (
        "the highlight never clears, so every opened entry stays marked")
