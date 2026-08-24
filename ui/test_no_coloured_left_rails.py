"""A coloured bar down the left edge of a block must not come back.

WHY THIS IS A TEST AND NOT A COMMENT. It was a comment. Theme.cs recommended "a thin left rail
+ small chip", that recommendation was read and followed, three rails were built, and the
operator's response was that they have said for months they dislike the look. A note in the
source is advisory; it was believed, and it was wrong. The regression class is specifically
"a future editor follows stale guidance", which a source sweep can catch and a reviewer's
memory cannot.

WHAT IT LOOKS FOR, AND WHY NOT JUST GEOMETRY. A thin left border is not the offence -- a 1px
neutral rule is an ordinary divider, and the markdown blockquote's grey left border is the
universal typographic convention for a quotation. The offence is thin-left geometry TOGETHER
with a status or accent colour, which is what turns a divider into a sticky note. Sweeping on
geometry alone would have flagged those legitimate uses, and the fix for that is an allowlist
-- and the lesson already recorded in this project is that hand-written allowlists fail open.
So the predicate is the conjunction, and there is no list to maintain.

If an exception ever genuinely needs one, the fail-closed shape is: the exact full source line,
a mandatory reason, and an assertion that the line still exists verbatim -- so a stale entry
fails the run instead of quietly widening the hole.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent
SOURCES = sorted(UI.glob("*.cs"))

#: Colours that make a bar a status rail rather than a rule. Neutral tokens (Border, Muted,
#: Faint, Line) are deliberately absent: a grey hairline is a divider and always allowed.
NON_NEUTRAL = re.compile(
    r"\b(Accent|AccentSoft|KeyAccent|Success|Warning|Danger|Info|"
    r"RailColor|StatusColor|StatusRail|statusBrush)\b"
)

#: A left-only border of any weight: new Thickness(N, 0, 0, 0) with N > 0.
LEFT_ONLY = re.compile(r"BorderThickness\s*=\s*new\s+Thickness\(\s*([1-9]\d*)\s*,\s*0\s*,\s*0\s*,\s*0\s*\)")

#: A standalone bar: a Border whose width is a handful of pixels.
NARROW_BAR = re.compile(r"new\s+Border\s*\{[^}]*\bWidth\s*=\s*([2-6])\b")

#: How far from the geometry a colour still counts as being on the same element. A Border's
#: brush is normally set in the same initializer or on the next line or two.
NEARBY = 3


def _strip_comments(text):
    """Comments describe rails; they must not be mistaken for building one.

    Five tests in this project have failed on their own prose. Removing it first is cheaper
    than wording every explanation around the pattern it explains.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _hits(path):
    lines = _strip_comments(path.read_text(encoding="utf-8", errors="replace")).splitlines()
    out = []
    for i, line in enumerate(lines):
        geom = LEFT_ONLY.search(line) or NARROW_BAR.search(line)
        if not geom:
            continue
        window = "\n".join(lines[max(0, i - NEARBY):i + NEARBY + 1])
        colour = NON_NEUTRAL.search(window)
        if colour:
            out.append((i + 1, colour.group(1), line.strip()[:90]))
    return out


def test_there_are_c_sharp_sources_to_sweep():
    """Fail closed. A sweep that silently found no files would pass for ever."""
    assert len(SOURCES) >= 3, [p.name for p in SOURCES]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_coloured_left_rail(path):
    hits = _hits(path)
    assert hits == [], (
        "a coloured left rail is being built in %s: %s -- put the status colour on the text it "
        "describes or in a chip" % (path.name, hits))


def test_the_guidance_that_caused_the_regression_is_still_corrected():
    """The last failure came from guidance, so the guidance is guarded too.

    A future edit that restores the old recommendation would make every rail above legitimate
    again in the eyes of whoever reads Theme.cs next.
    """
    theme = (UI / "Theme.cs").read_text(encoding="utf-8", errors="replace")
    assert "NO COLOURED LEFT RAILS ON CONTENT BLOCKS" in theme
    assert "there is no exception" in theme


def test_the_rail_width_token_is_gone():
    """The token is the reintroduction vector: a named width invites a bar to wear it."""
    for path in SOURCES:
        body = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        assert "RailW" not in body, "%s still references the rail width token" % path.name
