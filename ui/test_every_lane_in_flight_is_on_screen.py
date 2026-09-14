# -*- coding: utf-8 -*-
"""Two goals were running and the panel showed one of them, swapping as they progressed.

WHAT THE OPERATOR SAW. The directive band reported "ゴール (2)" and then printed a single goal
followed by "(他 1 lane)". As lanes advanced the band re-rendered and the goal it showed
CHANGED -- the view moved on its own, with no input. Reported as "これがちらちらと動く".

WHY IT MOVED. `goalTexts` is built by walking the worker list and collecting distinct goals, so
its order is the order the workers happen to arrive in; the band then rendered `goalTexts[0]`
and replaced everything else with a count. Nothing pinned which goal was first, so any refresh
that reordered the workers changed what was on screen. The count was right the whole time --
only the rendering discarded the list it had already assembled.

THE OPERATOR'S OWN FRAMING IS THE FIX: "fork 的な考えをすれば単にここに2つ並べればよいでしょ".
Concurrent lanes are concurrent. They go side by side, not behind a number.
"""
from __future__ import annotations

import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")


def _src():
    return io.open(COCKPIT, encoding="utf-8").read()


def _band():
    """The directive-band builder, located by the list it assembles rather than by a line
    inside it -- the goal text and the labels around it are what changes here."""
    src = _src()
    i = src.index("var goalTexts = new List<string>();")
    j = src.index("\n        // Meta line (started", i)
    return src[i:j]


def test_every_collected_goal_reaches_the_screen():
    """THE DEFECT, in one assertion: the band renders the whole list, not its first element."""
    band = _band()
    assert "string.Join(\"\\n\", goalTexts.ToArray())" in band, (
        "the directive band is rendering something other than the full goal list; with more "
        "than one lane in flight it will show one of them and the operator will watch it swap")


def test_the_lane_count_no_longer_stands_in_for_the_lanes():
    """`(他 N lane)` was the summary that replaced the goals. Its absence is the property --
    a count beside the list would be fine, a count INSTEAD of the list is what moved.

    COMMENTS ARE STRIPPED FIRST, and that is not tidiness. The first draft searched the raw
    text and matched the comment above the fix, which QUOTES the string it forbids -- the same
    trap as a substring scan hitting a docstring, hit for the third time in one day. What is
    being asserted is what the code DOES, so only code is looked at."""
    band = "\n".join(ln for ln in _band().splitlines()
                     if not ln.strip().startswith("//"))
    assert "lane)" not in band, (
        "the collapsed lane indicator is back in the goal text")
    assert "goalTexts[0]" not in band, (
        "the band is back to picking one goal out of the list, and nothing decides which")


def test_the_section_still_says_how_many():
    """Dropping the collapse must not drop the count: "ゴール (2)" is how an operator knows the
    list is complete rather than truncated."""
    band = _band()
    assert "ゴール (" in band and "Goals (" in band


def test_the_goal_block_can_show_more_than_one_line():
    """A joined list is only visible if the control renders newlines. A single-line TextBlock
    would show the first goal again, by a different route."""
    src = _src()
    i = src.index("goalTb.Text = goalDisplay;")
    arm = src[i:i + 400]
    assert "TextWrapping.Wrap" in arm, (
        "the goal block does not wrap, so a multi-goal list collapses to its first line")
