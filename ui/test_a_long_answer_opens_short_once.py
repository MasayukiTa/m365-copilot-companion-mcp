# -*- coding: utf-8 -*-
"""A long answer buried everything under it, and the obvious fix would have missed the case.

MEASURED BEFORE BUILDING ANYTHING. An external review recommended folding long TOOL OUTPUT, with
the reference implementation's own constants to match (60 lines / 4,000 characters). On this
machine's transcripts that is not where the length is:

    assistant turns with text   138
    characters                  median 280, p90 1,130, max 2,282
    over 1,000 characters       20  (14%)
    mentioning a tool call      11  (8%)

Tool output is 8% of turns. Machinery aimed at it would have been aimed at almost nothing, while
the 14% of ordinary prose over a thousand characters kept pushing the rest of the conversation
off the screen. So the threshold is the p90 of what is actually there: below it nothing changes,
above it the body opens clipped with a line saying how much is hidden.

THREE RULES TAKEN FROM THE REVIEW, each a way this goes wrong:

  * 「要約は原文への入口として置きます。原文を置き換えたり」 -- the fold is the same text,
    clipped. Never a paraphrase, never a summary of it.
  * 「閲覧中の過去のまとまりを突然折りたたんだりしないこと」 -- there is no re-fold. Once the
    reader asks for the text, nothing takes it back.
  * 「自動折りたたみは初回表示時などに限定し」 -- folding is decided while rendering a turn,
    not on the refresh path, so a tick cannot re-fold what is open.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")


def _src():
    return io.open(CHAT, encoding="utf-8").read()


def _code_only(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


def _render_body():
    src = _src()
    i = src.index("void RenderAssistantBody(")
    j = src.index("\n    //: Above this many characters", i) if "\n    //: Above this many characters" in src[i:] \
        else src.index("\n    static void FillFlowDocument", i)
    return _code_only(src[i:j])


# ── the threshold is the measurement ──────────────────────────────────────────────────────

def test_the_threshold_is_declared_and_near_the_measured_p90():
    """A number nobody can trace is a number the next person will change for a feeling. p90 was
    1,130; a threshold far below it would grow a control on answers that were always fine, and
    far above it would never fire."""
    m = re.search(r"ASSISTANT_FOLD_CHARS\s*=\s*(\d+)", _src())
    assert m, "the fold threshold is no longer declared"
    v = int(m.group(1))
    assert 800 <= v <= 1600, (
        "the fold threshold is %d; the measured p90 of assistant answers was 1,130" % v)


def test_short_answers_are_untouched():
    """86% of turns were already under the threshold. They must not gain a control."""
    body = _render_body()
    assert "bool folds = plain.Length > ASSISTANT_FOLD_CHARS;" in body, (
        "folding no longer depends on the length, so it applies to everything")
    assert "if (folds)" in body, "the fold control is added unconditionally"


# ── the fold is an entrance, not a replacement ────────────────────────────────────────────

def test_the_folded_body_is_the_same_text_clipped():
    """A card or a panel that paraphrases can be wrong about what the agent said. This one
    takes a prefix of the very text it would otherwise show."""
    body = _render_body()
    assert "plain.Substring(0, ASSISTANT_FOLD_CHARS)" in body, (
        "the folded body is being produced some other way than by clipping the original")
    assert "string full = plain;" in body, "the full text is no longer kept for the reveal"


def test_the_control_says_how_much_is_hidden():
    """"Show the rest" alone leaves the reader guessing whether it is a line or a page, which is
    the question they are trying to answer before clicking."""
    body = _render_body()
    assert "int hidden = full.Length - ASSISTANT_FOLD_CHARS;" in body
    assert "あと " in body and "more characters" in body


def test_opening_it_is_one_way():
    """A control that folds it back again is the panel changing under the reader, which is the
    family of defect this window has spent the day removing."""
    body = _render_body()
    i = body.index("more.MouseLeftButtonUp")
    arm = body[i:i + 700]
    assert "moreRef.Visibility = Visibility.Collapsed;" in arm, (
        "the reveal control survives the reveal, so the answer can be folded again")
    assert "rtbRef.Document = BuildFlowDocument(fullRef" in arm, (
        "opening a folded answer no longer rebuilds it from the full text")


def test_both_bodies_are_laid_out_by_the_same_code():
    """The folded body and the full body must not drift apart. The layout was inline, which is
    why unfolding had no way to re-render without a second copy of it."""
    src = _src()
    assert "static void FillFlowDocument(FlowDocument doc, string plain)" in src
    assert src.count("FillFlowDocument(") >= 3, (
        "the extracted layout is not being used by both paths")


def test_folding_is_decided_while_rendering_a_turn():
    """Not on the refresh path. A tick that re-folded an answer the reader had opened would be
    the re-fold the review forbids, arriving by a different route."""
    src = _code_only(_src())
    i = src.index("void RefreshFleetSnapshot()")
    j = src.index("\n    ", src.index("StickToEnd();", i))
    assert "ASSISTANT_FOLD_CHARS" not in src[i:j], (
        "the refresh path is making folding decisions")
