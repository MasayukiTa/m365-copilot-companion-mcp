"""No character may stand in for an icon.

THE RULE WAS ALREADY HERE, AND THAT IS THE POINT. "Material Symbols glyphs (vector paths, NO
emoji)" is written five times across three of these files. One of those five sits in the file
that carried a trigram for a hamburger, a gear, a magnifier, a test tube and a reload arrow. A
conversion was even done once and recorded in a comment -- "replacing the three full-width emoji
text buttons" -- and it stopped three buttons short of the header it was written in. Stating a
rule next to the code that breaks it is what this repository keeps doing; this file is the
version that cannot be ignored.

WHY A CHARACTER IS NOT AN ICON. Its weight, its size, its vertical placement and whether it
exists at all come from whichever font on the machine happens to contain it. It cannot match the
drawn glyphs beside it, and on a machine without that font it is a box.

WHAT IS DELIBERATELY ALLOWED. U+2192 RIGHTWARDS ARROW inside a sentence -- "最大タブ →",
"古い→新しい", "検知 {0} → {1}後に再署名" -- is punctuation. Banning it would damage the writing
rather than the interface, so the allowance is narrow and explicit: the arrow, and only the
arrow, and the test still fails if it appears as a whole control's content.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent
SOURCES = sorted(UI.glob("*.cs"))

#: Blocks that hold pictographs, dingbats, arrows, geometric shapes and the emoji planes.
BANNED_RANGES = [
    (0x2190, 0x21FF), (0x2300, 0x23FF), (0x2460, 0x24FF), (0x25A0, 0x25FF),
    (0x2600, 0x27BF), (0x27F0, 0x27FF), (0x2900, 0x297F), (0x2B00, 0x2BFF),
    (0x4DC0, 0x4DFF), (0xFE0F, 0xFE0F), (0x1F000, 0x1FAFF),
]

#: The one exception, and the reason it is one. A rightwards arrow reads as punctuation inside a
#: sentence in both languages this UI ships. Nothing else is exempt -- if a second exception ever
#: looks necessary, that is the moment to add a glyph instead.
PUNCTUATION = {"→"}


def _banned(ch):
    o = ord(ch)
    return any(a <= o <= b for a, b in BANNED_RANGES)


def _live_lines(path):
    """Source lines with comments dropped.

    The prose above names the characters it forbids, and so do the comments in the C#. Six tests
    in this project have failed on their own explanation; stripping first is cheaper than
    wording every sentence around the thing it describes.
    """
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("//"):
            continue
        yield i, re.sub(r"//.*$", "", line)


def test_there_are_sources_to_sweep():
    """Fail closed: a sweep that found no files would pass for ever."""
    assert len(SOURCES) >= 3, [p.name for p in SOURCES]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_character_stands_in_for_an_icon(path):
    found = []
    for lineno, line in _live_lines(path):
        for ch in sorted({c for c in line if _banned(c) and c not in PUNCTUATION}):
            found.append("%s:%d U+%04X" % (path.name, lineno, ord(ch)))
    assert found == [], (
        "a character is doing an icon's job in %s: %s -- add the glyph to "
        "ui/assets/material_glyphs.json and draw it with MakeIcon" % (path.name, found))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_the_allowed_arrow_is_never_a_whole_control(path):
    """An arrow inside a sentence is punctuation; an arrow that IS the label is an icon.

    This is the loophole the exemption above would otherwise open, and it is the exact shape the
    send button had: `send.Content = "↳"`, a typographic mark used as a control.
    """
    bad = []
    for lineno, line in _live_lines(path):
        for m in re.finditer(r'(Content|Text)\s*=\s*"([^"]*)"', line):
            v = m.group(2).strip()
            if v and all(c in PUNCTUATION or c.isspace() for c in v):
                bad.append("%s:%d %r" % (path.name, lineno, v))
    assert bad == [], "a bare arrow is being used as a control's content: %s" % bad


def test_the_glyph_subset_covers_what_the_ui_asks_for():
    """Every MakeIcon name must exist in the subset.

    MakeIcon returns an EMPTY BORDER for a name it does not have -- silently, by design, so a
    missing file cannot crash the app. That same kindness means a typo or a glyph nobody added
    shows up as a blank space where a control should be, and nothing anywhere says so.
    """
    import json
    data = json.loads((UI / "assets" / "material_glyphs.json").read_text(encoding="utf-8-sig"))
    have = set(data["glyphs"])
    asked = set()
    for path in SOURCES:
        for _, line in _live_lines(path):
            asked.update(re.findall(r'MakeIcon\(\s*"([a-z_]+)"', line))
            for m in re.finditer(r'MakeIcon\(\s*[^,)]*\?\s*"([a-z_]+)"\s*:\s*"([a-z_]+)"', line):
                asked.update(m.groups())
    assert asked, "found no MakeIcon call sites -- the sweep is not reading what it thinks"
    assert asked <= have, "MakeIcon asks for glyphs the subset lacks: %s" % sorted(asked - have)
