# -*- coding: utf-8 -*-
"""The self-improvement check's health-strip dot must not speak in engineering jargon.

## The complaint (verbatim, from the owner)

"今なぜか凍結セットの信号が付くようになったけどこれなぜいれたの? 凍結セットって日本語では
だれもわかりませんわ少なくとも" -- roughly: a dot now shows "凍結セット" ("frozen set"), a
term that means the checksum-guarded set of judge files to the engineer who named it and
nothing to anyone else. The tool has to be usable by anyone in the company, not just the
person who wrote the self-improvement loop.

## What this checks

Every operator-visible string in ui/FleetCockpit.cs and ui/SelfImproveDashboard.cs -- labels,
tooltips, dialog text -- must use plain language ("自己改善の安全確認" / "Self-improvement
check") instead of "凍結セット" / "Frozen set". The internal MECHANISM is still called the
frozen set in code comments and identifiers (FrozenMatches, frozen_baseline.json, FROZEN_MANIFEST
in relay/selfimprove/frozen.py) -- that is an engineering name for an engineering concept, and
renaming it everywhere is a much bigger, unrelated change. What must not leak is the term
appearing in something a non-engineer reads on screen.

## How "operator-visible" is decided

A source line is live (not a comment) the same way ui/test_no_emoji_in_chrome.py decides it:
block comments stripped, whole-line `//` comments dropped, trailing `//` comments cut off the
end of a code line. If the banned term still appears in what remains, it is reachable from a
string literal a person can see -- not just prose in a comment explaining the mechanism.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent
SOURCES = [UI / "FleetCockpit.cs", UI / "SelfImproveDashboard.cs"]

#: Case-sensitive Japanese term (case doesn't apply) + case-insensitive English term. Both
#: named literally, not derived, so a rename of one does not silently stop checking the other.
BANNED_JA = "凍結セット"
BANNED_EN_RE = re.compile(r"frozen set", re.IGNORECASE)


def _live_lines(path: Path):
    """Source lines with comments dropped -- see ui/test_no_emoji_in_chrome.py's helper of the
    same name, which this mirrors so the two checks agree on what "visible" means."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("//"):
            continue
        yield i, re.sub(r"//.*$", "", line)


def test_there_are_sources_to_sweep():
    """Fail closed: a sweep that found no files would pass forever."""
    assert all(p.is_file() for p in SOURCES), [str(p) for p in SOURCES if not p.is_file()]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_operator_visible_string_says_frozen_set(path):
    found = []
    for lineno, line in _live_lines(path):
        if BANNED_JA in line or BANNED_EN_RE.search(line):
            found.append("%s:%d: %s" % (path.name, lineno, line.strip()))
    assert found == [], (
        "an operator can still see the untranslated term \"frozen set\" / \"凍結セット\": %s -- "
        "use plain language instead, e.g. \"自己改善の安全確認\" / \"Self-improvement check\" "
        "(see hs_frozen* / auth_intact in T())" % found)


def test_the_replacement_wording_is_actually_present():
    """A guard against the trivial way to pass the test above: deleting the label instead of
    replacing it. The plain-language label has to exist somewhere an operator reads it."""
    fleet = (UI / "FleetCockpit.cs").read_text(encoding="utf-8", errors="replace")
    dash = (UI / "SelfImproveDashboard.cs").read_text(encoding="utf-8", errors="replace")
    assert "自己改善の安全確認" in fleet, "hs_frozen's Japanese label went missing, not just its jargon"
    assert "Self-improvement check" in fleet, "hs_frozen's English label went missing, not just its jargon"
    assert "自己改善の安全確認" in dash, "auth_intact's Japanese label went missing, not just its jargon"
    assert "Self-improvement check" in dash, "auth_intact's English label went missing, not just its jargon"
