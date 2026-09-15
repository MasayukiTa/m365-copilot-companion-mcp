# -*- coding: utf-8 -*-
"""An answer must not re-wrap at the moment it stops streaming.

A streaming answer is drawn in a plain TextBox (`MakeText`); the settled one is a RichTextBox
over a FlowDocument (`RenderAssistantBody`), because a TextBlock cannot be selected and a TextBox
has no line height. The swap itself is fine. What is not fine is the two disagreeing about
anything that decides where a line breaks -- and they did:

    streaming : FontFamily "Segoe UI",                  Padding (2, 0, 0, 0)
    settled   : FontFamily "Segoe UI Variable, Segoe UI", Padding (0, 0, 0, 0)

Different faces have different advance widths, so every line moved when an answer finished, and
the two pixels of left padding moved them again. An external review of this window named this
exact case and said to treat it as a READING POSITION problem rather than a cosmetic one: the
reader is mid-sentence when the text shifts under them.

This checks the source rather than the rendering because the rendering needs a desktop -- so it
asserts the only thing that can be checked without one, and the strongest thing available: both
paths take their face, size and padding from the SAME named constants, so they cannot drift
apart without someone deliberately unpicking them.
"""
from __future__ import annotations

import io
import os
import re

SOURCE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CopilotChat.cs")
SOURCE = io.open(SOURCE_PATH, encoding="utf-8", errors="replace").read()


def _body(start, end):
    i = SOURCE.index(start)
    j = SOURCE.index(end, i)
    return SOURCE[i:j]


def test_the_shared_constants_exist():
    assert "static readonly FontFamily BODY_FACE" in SOURCE
    assert "const double BODY_SIZE" in SOURCE
    assert "static readonly Thickness BODY_PAD" in SOURCE


def test_the_streaming_box_takes_all_three_from_them():
    body = _body("TextBox MakeText(string text)", "\n    // the sibling app chat loader")
    for name in ("BODY_FACE", "BODY_SIZE", "BODY_PAD"):
        assert name in body, "MakeText does not use %s" % name
    assert 'new FontFamily("Segoe UI")' not in body, (
        "MakeText still names a face of its own; that is the mismatch this fixes")


def test_the_settled_renderer_takes_all_three_from_them():
    body = _body("void RenderAssistantBody(Panel content, StackPanel outer, string text)",
                 "\n    static FlowDocument BuildFlowDocument")
    for name in ("BODY_FACE", "BODY_SIZE", "BODY_PAD"):
        assert name in body, "RenderAssistantBody does not use %s" % name


def test_no_rendering_path_still_hard_codes_the_size():
    """A literal 14 anywhere in these three bodies is a future drift waiting to happen."""
    bodies = [
        _body("TextBox MakeText(string text)", "\n    // the sibling app chat loader"),
        _body("void RenderAssistantBody(Panel content, StackPanel outer, string text)",
              "\n    static FlowDocument BuildFlowDocument"),
        _body("static FlowDocument BuildFlowDocument", "\n    //: THE TWO RENDERINGS"),
    ]
    for body in bodies:
        assert not re.search(r"FontSize\s*=\s*14\b", body), (
            "a rendering path still hard-codes FontSize = 14:\n%s" % body[:200])


def test_the_reason_is_written_down_where_the_constants_are():
    """So the next person to 'tidy up' the duplicate face name knows what it costs."""
    i = SOURCE.index("static readonly FontFamily BODY_FACE")
    j = SOURCE.rindex("//: THE TWO RENDERINGS", 0, i)
    # WHITESPACE-NORMALISED, because the phrase is split across a comment line break and a
    # test that pins where a sentence wraps is a test that fails the next time someone reflows
    # a paragraph. This repository has been bitten by that anchor several times over.
    preamble = " ".join(SOURCE[j:i].replace("//", " ").split())
    assert "wrap" in preamble, "the comment does not say what goes wrong"
    assert "READING POSITION" in preamble, "the comment does not say why it matters"
