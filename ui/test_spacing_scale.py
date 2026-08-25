"""Structural spacing steps in fours, and the set of values in use may shrink but not grow.

WHAT WAS MEASURED. 2,266 spacing numbers across these four files, spread over 28 distinct values,
with essentially every integer from 1 to 16 in use. That is not a scale, it is a continuum. It is
the same failure the corner radii had -- a system declared in Theme.cs and disregarded at the call
site -- on thirty times the surface, and nobody could have noticed by looking: no single 9 or 11
is wrong, and that is exactly why there were eventually twenty-eight of them.

WHAT THIS FILE ENFORCES, AND WHAT IT DELIBERATELY DOES NOT. Above the optical range every value
must be a multiple of four. Below it -- 1 through 7 -- values are left alone on purpose. That is
where you stop laying out and start adjusting for the eye: the pixel that centres an icon against
a cap height, the two that keep a dense row off its divider. Rounding those to four would damage
the rows this app is mostly made of, so the rule does not pretend to cover them.

The second test is the one that matters in a year. The problem was never any particular number;
it was that nothing stopped the next one. So the inventory is recorded, and it may shrink but
never grow -- a ratchet, not an allowlist. Deleting a value from the recorded set is meant to
happen; adding one requires deciding to, in this file, where the reason can be written down.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent
SOURCES = sorted(UI.glob("*.cs"))

#: Values below this are optical adjustment, not layout. See the note above.
OPTICAL_MAX = 7

#: Every spacing value in use, as measured. May shrink. Adding to it is a decision, not a fix.
IN_USE = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 28, 32, 36, 40, 80}

THICKNESS = re.compile(r"new Thickness\(([^)]*)\)")
NUMBER = re.compile(r"\d+(\.\d+)?$")


def _values():
    """Every literal number passed to a Thickness, with where it came from.

    Comments are dropped first: this file's own prose names the values it forbids, and six tests
    in this project have failed on their own explanation.
    """
    out = []
    for path in SOURCES:
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
        for i, raw in enumerate(text.splitlines(), 1):
            if raw.strip().startswith("//"):
                continue
            line = re.sub(r"//.*$", "", raw)
            for m in THICKNESS.finditer(line):
                args = [a.strip() for a in m.group(1).split(",")]
                if not all(NUMBER.match(a) for a in args):
                    continue          # computed or token-based; not a literal to police
                for a in args:
                    out.append((float(a), path.name, i))
    return out


def test_there_is_something_to_measure():
    """Fail closed: a sweep that matched nothing would pass for ever."""
    vals = _values()
    assert len(vals) > 1500, "only %d spacing literals found -- the sweep is not reading the UI" % len(vals)


def test_structural_spacing_steps_in_fours():
    bad = sorted({(v, f, i) for v, f, i in _values() if v > OPTICAL_MAX and v % 4 != 0})
    assert bad == [], (
        "spacing above the optical range must be a multiple of 4: %s" % bad[:12])


def test_the_set_of_values_in_use_never_grows():
    used = {v for v, _, _ in _values()}
    new = sorted(used - IN_USE)
    assert new == [], (
        "new spacing values appeared: %s -- pick one already in use, or add it to IN_USE here "
        "with the reason" % new)


def test_the_recorded_set_has_no_dead_entries():
    """A stale entry is how a ratchet quietly turns back into an allowlist.

    If a value stops being used, the record must lose it -- otherwise the set only ever grows in
    effect, and the next person reads it as permission rather than as a measurement.
    """
    used = {v for v, _, _ in _values()}
    dead = sorted(IN_USE - used)
    assert dead == [], "IN_USE lists values nothing uses any more: %s -- remove them" % dead


def test_the_scale_is_written_where_the_tokens_live():
    theme = (UI / "Theme.cs").read_text(encoding="utf-8", errors="replace")
    for name in ("Sp1", "Sp2", "Sp3", "Sp4", "Sp5", "Sp6"):
        assert ("public const double %s" % name) in theme, name
