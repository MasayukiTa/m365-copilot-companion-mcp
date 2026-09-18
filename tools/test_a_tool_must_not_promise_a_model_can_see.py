# -*- coding: utf-8 -*-
"""Nothing may tell an agent that read_image lets a model look at a picture.

MEASURED 2026-09-17, one fleet worker, one picture carrying six characters that appear in no
filename, no path and no instruction. Chance of guessing: one in 32**6.

    10:59:44  read_image(card.png)      -> the worker then answered 7F3A9C. FABRICATED.
    10:59:44  ocr_image(card.png)       -> "PFESBT". CORRECT.
    10:58-11:00  run_python + PIL       -> the letter shapes, read one by one. CORRECT.
    ANALYZE:                              never fired, although the instruction named it.

read_image returned a `str`; FastMCP serialises a str as a TEXT content block; no client in
this stack rendered that as an image. So the tool sent 142,642 characters on a median call
(measured over 424 calls) and showed nobody anything -- and the worker that called it believed
it had looked, and said so twice, in both turns, describing what it had supposedly seen. The
refuter caught it both times, which is the only reason a correct answer came back at all.

THE DOCSTRINGS LOOKED LIKE THE DEFECT, AND THEY WERE THE SECOND ONE. "so a vision model can
see it", and three separate tools plus the architecture document instructing a worker to verify
its own output by calling it -- a check that cannot fail and cannot pass. Those claims are
still false about a text block and are still pinned out, below.

THE FIRST DEFECT WAS THE RETURN TYPE, AND IT IS FIXED (2026-09-18). read_image returns a
fastmcp.utilities.types.Image, which serialises as an IMAGE content block. The conclusion drawn
here at first -- "stop using this tool" -- was wrong, and this file said so in its own words by
declining to pin the return value: removing a capability because its plumbing is broken makes
the broken plumbing permanent. A visual path is not optional. Computer-use decides where to
click and a one-pixel error is a miss, so nothing built on OCR or on pixel arithmetic replaces
seeing the screen.

So what is pinned here now is BOTH halves: that read_image returns something a client can
actually render, and that nothing goes back to promising a TEXT block does. The alternatives
below are still the right answer for the questions they answer -- ocr_image is exact and cheap
on flat text, run_python is exact and cheapest on pixel facts -- and ANALYZE is still the only
route to Copilot's own eyes, on a model nobody can pick.
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


def test_read_image_returns_something_a_client_can_render():
    """THE CAUSE, PINNED. Not the docstring -- the value. A str comes back as a TEXT block and
    is the whole original defect; an Image comes back as an IMAGE block."""
    import base64

    from PIL import Image as PILImage

    import tools.image_ops as IO

    tmp = os.path.join(os.environ.get("TEMP", "."), "_promise_probe.png")
    PILImage.new("RGB", (80, 40), (200, 30, 30)).save(tmp)
    try:
        out = IO.read_image(tmp)
        assert not isinstance(out, str), \
            "read_image is returning text again; nothing renders a text block as a picture"
        data = getattr(out, "data", None)
        assert data, "the result carries no image bytes: %r" % (out,)
        PILImage.open(io.BytesIO(data)).load()      # it is a real image, not a description
        assert base64.b64encode(data)               # and it is what an ImageContent carries
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def test_read_image_still_says_what_it_actually_returns():
    """Corrected, not merely emptied: a docstring that stops promising and says nothing is how
    the next person re-invents the promise. It has to carry the history too -- what it used to
    return, why that was wrong, and what the alternatives are still for."""
    body = _read("tools/image_ops.py")
    assert "ocr_image" in body and "run_python" in body
    assert "142,642" in body, "the cost was measured; a number nobody records gets argued about"
    assert "TEXT content block" in body, \
        "the docstring no longer records what it used to return, so the mistake can return"
    assert "model_picker=None" in body, \
        "ANALYZE is named without the caveat that it runs on a model nobody can pick"
