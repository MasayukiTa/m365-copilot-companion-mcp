# -*- coding: utf-8 -*-
"""Nothing may tell an agent that read_image lets a model look at a picture.

MEASURED 2026-09-17, one fleet worker, one picture carrying six characters that appear in no
filename, no path and no instruction. Chance of guessing: one in 32**6.

    10:59:44  read_image(card.png)      -> the worker then answered 7F3A9C. FABRICATED.
    10:59:44  ocr_image(card.png)       -> "PFESBT". CORRECT.
    10:58-11:00  run_python + PIL       -> the letter shapes, read one by one. CORRECT.
    ANALYZE:                              never fired, although the instruction named it.

read_image returns a `str`; FastMCP serialises a str as a TEXT content block; no client in
this stack renders that as an image. So the tool sends 142,642 characters on a median call
(measured over 424 calls) and shows nobody anything -- and the worker that called it believed
it had looked, and said so twice, in both turns, describing what it had supposedly seen. The
refuter caught it both times, which is the only reason a correct answer came back at all.

THE DOCSTRINGS WERE THE DEFECT. "so a vision model can see it", and three separate tools plus
the architecture document instructing a worker to verify its own output by calling it. A
worker that follows those instructions performs a check that cannot fail and cannot pass.

This does not pin the RETURN VALUE. tools/test_a_cap_in_bytes_never_bound_the_thing_that_broke
owns that, as the lesson of an earlier incident, and removing a contract with its own history
is a decision to take deliberately rather than as a side effect. What is pinned here is that
nothing promises what was measured to be false.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Files an agent reads before deciding how to check its own work.
_STEERING = (
    "tools/image_ops.py",
    "tools/code_exec.py",
    "tools/render_ops.py",
    "tools/pptx_ops.py",
    "tools/screenshot_ops.py",
    "tools/screen_ops.py",
    "docs/ARCHITECTURE.md",
    "docs/TROUBLESHOOTING.md",
)

#: Claims measured false on 2026-09-17. Matched case-insensitively.
_FALSE_PROMISES = (
    r"so a vision model can see",
    r"directly consumable by vision",
    r"read_image で Opus に直接読ませる",
)


def _read(rel):
    return io.open(os.path.join(REPO, rel), encoding="utf-8", errors="replace").read()


@pytest.mark.parametrize("rel", _STEERING)
def test_nothing_claims_read_image_shows_a_model_the_picture(rel):
    body = _read(rel)
    for pattern in _FALSE_PROMISES:
        assert not re.search(pattern, body, re.I), \
            "%s still promises what was measured false: %r" % (rel, pattern)


@pytest.mark.parametrize("rel", ("tools/code_exec.py", "tools/render_ops.py",
                                 "tools/pptx_ops.py", "docs/ARCHITECTURE.md"))
def test_self_verification_points_at_something_that_can_answer(rel):
    """A worker told to 'self-verify with read_image' performs a check that cannot fail and
    cannot pass. Each of these places now has to name a tool that answered on the measurement:
    ocr_image, or run_python for pixel facts."""
    body = _read(rel)
    assert ("ocr_image" in body) or ("run_python" in body), \
        "%s tells a worker to verify an image without naming a tool that can" % rel


def test_the_one_path_that_shows_a_picture_is_named_where_it_is_needed():
    """ANALYZE is the only route in this repository that puts a file in front of Copilot --
    relay/agent_profiles.py attaches it to a real <input type=file>. It never fired on the
    measurement even though the instruction named it, which is its own open question; what
    must not happen is a worker looking for that route and not finding it written down."""
    for rel in ("tools/image_ops.py", "docs/ARCHITECTURE.md"):
        assert "ANALYZE:" in _read(rel), "%s does not name the one route that works" % rel


def test_read_image_still_says_what_it_actually_returns():
    """Corrected, not merely emptied: a docstring that stops promising and says nothing is how
    the next person re-invents the promise."""
    body = _read("tools/image_ops.py")
    assert "TEXT, not a picture" in body
    assert "ocr_image" in body and "run_python" in body
    assert "142,642" in body, "the cost was measured; a number nobody records gets argued about"
