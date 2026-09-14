# -*- coding: utf-8 -*-
"""Three scroll regions were stacked in one direction, and the conversation could not be read.

WHAT THE OPERATOR SAW: 「これそれぞれが枠に入っているせいで、開いてしまえば一番下まで
スクロールしないと次のが見られない」. Each turn on a worker card was a TextBox capped at
`MaxHeight = 90` with its own scrollbar, inside a `ScrollViewer` capped at 240 with another, and
that inside the card list's own scroller. To reach the second turn you scrolled the first to its
end, then the panel, and the card moved under you while you did it.

THE PRINCIPLE, from the review asked about it: 「問題はスクロールの存在ではなく、同じ操作方向に
異なる本文領域が重なっていること」. Raising the caps does not fix that and lowering them is the
same thing smaller -- a monitoring card cannot hold hundreds of lines without becoming the thing
it was supposed to summarise, and one long-running worker would push every other card off screen.

SO THE CARD LISTS WHAT HAPPENED AND HOLDS NO BODY. Each entry is who, one trimmed line, and the
size that tells you whether to open it. The full text opens where reading belongs. The review
weighed the alternatives and rejected each for a stated reason: an unbounded body (one lane eats
the view), in-card expansion (the card and everything under it jumps), a modal (it covers the
monitor the card exists to be).

WHAT THIS FILE DOES NOT CLAIM. Opening an entry at its position in the chat window is not built
yet; this pins the card's half -- that it stopped being a reading surface.
"""
from __future__ import annotations

import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _src():
    return io.open(COCKPIT, encoding="utf-8").read()


def _mini_thread():
    """Located by the method NAME, not by its full signature.

    The first version pinned `MiniThread(string transcriptPath)` exactly, and every assertion in
    this file broke the moment the method gained the two arguments it needs to open an entry in
    the chat window -- four failures reading "substring not found", about a restructure that was
    working. The subject is the method; its parameter list is free to change."""
    src = _src()
    i = src.index("UIElement MiniThread(")
    j = src.find("\n    static string FirstMeaningfulLine", i)
    assert j > 0, "MiniThread's end marker moved; re-derive this helper"
    return src[i:j]


def _code_only(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


# ── the card holds no scrolling body ──────────────────────────────────────────────────────

def test_the_card_has_no_scroll_region_of_its_own():
    """THE DEFECT, in one assertion. Neither the per-entry box nor the panel around it."""
    body = _code_only(_mini_thread())
    assert "ScrollViewer" not in body, (
        "the worker card is hosting a scroll region again -- stacked on the card list's own "
        "scroller, which is what made the conversation unreadable")
    assert "VerticalScrollBarVisibility" not in body


def test_no_entry_is_given_a_height_cap():
    """A height cap IS the scrollbox: it is how a body gets clipped into something the reader
    has to scroll inside. The entry is one trimmed line instead."""
    body = _code_only(_mini_thread())
    assert "MaxHeight" not in body, (
        "an entry is height-capped again; that is the scroll region under another name")
    assert "TextTrimming.CharacterEllipsis" in body, (
        "the entry line no longer trims -- a wrapped body is a body")
    assert "TextWrapping.NoWrap" in body


def test_the_entry_says_how_big_the_thing_is():
    """The reason to open an entry has to be visible without opening it. A scrollbar is not
    that: it has to be discovered, and it says "there is more" without saying how much."""
    body = _code_only(_mini_thread())
    assert "lines > 1" in body and "本文 " in body


def test_what_is_not_listed_is_counted():
    """Showing three and silently dropping the rest would be the same lie the scrollbox told."""
    body = _code_only(_mini_thread())
    assert "total > turns.Count" in body, "entries beyond the list are no longer accounted for"
    assert "ほか " in body


def test_the_list_is_short_by_construction():
    """Whatever the constant becomes, it must stay a handful: the card sits beside the state and
    the controls, and a long list makes it the reading surface again by a slower route."""
    src = _src()
    m = re.search(r"MINI_THREAD_ENTRIES\s*=\s*(\d+)", src)
    assert m, "the entry count is no longer declared"
    assert 1 <= int(m.group(1)) <= 5, "the card lists %s entries" % m.group(1)
    assert "ReadLastTurns(transcriptPath, MINI_THREAD_ENTRIES)" in src, (
        "the reader is not bounded by the declared count")


# ── the label is a label, not an invention ────────────────────────────────────────────────

def test_the_summary_line_is_taken_and_not_written():
    """A card that paraphrases is a card that can be wrong about what an agent said. It takes
    the first line that carries text -- skipping blanks and a bare heading marker -- and trims."""
    src = _src()
    i = src.index("static string FirstMeaningfulLine(string body, int max)")
    body = _code_only(src[i:i + 1200])
    assert "Substring(0, max)" in body, "the line is no longer trimmed to a bound"
    assert "ln.Length == 0" in body, "blank lines are no longer skipped"
    # markdown markers are stripped from the LABEL only; the body is untouched elsewhere
    assert 'Replace("**", "")' in body


def test_the_count_of_turns_fails_to_zero():
    """A wrong "ほか N 件" is worse than none: it is a claim about a conversation the reader
    cannot see. On any failure the count is 0 and the line is not rendered at all."""
    src = _src()
    i = src.index("int CountTurns(string transcriptPath)")
    body = _code_only(src[i:i + 900])
    assert "catch (Exception) { return 0; }" in body
    assert "File.Exists(transcriptPath)" in body
