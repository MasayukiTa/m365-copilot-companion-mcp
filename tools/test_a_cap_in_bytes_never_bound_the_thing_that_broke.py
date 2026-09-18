# -*- coding: utf-8 -*-
"""read_image must be bounded in the currency the caller spends.

WHAT IT COST. `.fleet/tool_events.jsonl`, 424 read_image calls: median 142,642 characters
returned, p90 1,166,294, max 1,762,326. One call could put over a million and a half
characters into a conversation. The guard that was supposed to prevent this, MAX_BYTES, caps
the FILE at 8 MB -- about 10.7 million characters of base64 -- so it had never once been the
binding constraint.

WHY IT LOOKED SAFE. read_image takes max_dimension=1600 and downscales to it, which reads
like a size limit. It only fires when the image is LARGER than that, and a desktop capture is
1400x788 -- so the common case was encoded untouched. Measured on a real capture: 1400x788
unresized is 205,240 characters; 1200 is 176,460; 1024 is 141,724; 900 is 113,996.

WHAT IT BROKE. Screen tasks died of conversation token limits -- 10 of 18 worker transcripts
in one run, and the recycle that follows opens a fresh conversation, which until 2026-09-15
also opened a LOCKED one. Seventeen read_image calls in a single 34-minute run is the
observed volume; at the median that is 2,424,914 characters.

WHAT THESE TESTS DO NOT CLAIM. The ceiling is not a fix for the loop on its own -- seventeen
calls at the ceiling is still over two million characters. The count is the larger lever, and
it lives in screen_look's window crop and in preferring screen_windows (median 506 characters,
282 times cheaper) when the question is "what is open". What is held here is narrower and
worth holding: no single call can be unbounded.
"""
from __future__ import annotations

import base64
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import image_ops as IO  # noqa: E402

PIL = pytest.importorskip("PIL.Image", reason="the budget needs Pillow to resize")


# ---- reading the result, whatever shape it arrives in ------------------------------------
#
# read_image returned a `data:image/...;base64,...` STRING until 2026-09-18, and these tests
# measured that string's length because that was the currency the caller spent. The string was
# the defect: FastMCP serialises a str as a TEXT block, so the picture was never a picture, and
# a worker that called it answered about an image it had not seen. It returns a
# fastmcp.utilities.types.Image now.
#
# The BUDGET IS THE SAME QUANTITY. An ImageContent block carries the bytes base64-encoded, so
# what these tests bound -- how much of the conversation one call costs -- is the base64 length
# either way. Read it off whichever shape came back rather than restating the ceiling.

def _payload_bytes(out):
    """The image bytes, from an Image content block or from a legacy data URI."""
    data = getattr(out, "data", None)
    if data is not None:
        return data
    assert isinstance(out, str), "unexpected read_image return: %r" % type(out)
    assert "base64," in out, out[:200]
    return base64.b64decode(out.split("base64,", 1)[1])


def _payload_chars(out):
    """What one call costs, in the currency the ceiling is written in."""
    return len(base64.b64encode(_payload_bytes(out)))


def _is_image_result(out):
    """It came back as something a client can render, not as text pretending to be one."""
    return getattr(out, "data", None) is not None


def _write(tmp_path, size, name="shot.png", block=4):
    """An image that compresses roughly like a screenshot, which is what is being bounded.

    THE TWO WRONG CHOICES, both of which make a passing test meaningless:

      A FLAT image. A solid-colour PNG of any dimension encodes to a few hundred bytes, so the
      test would pass with the budget deleted -- it would be measuring PNG's run-length
      coding, not read_image.

      PURE PER-PIXEL NOISE. It cannot be compressed at ANY width, so no size above the
      readability floor ever meets the budget and the call correctly returns the floor image.
      The first version of this file used noise and failed at 293,562 characters, which was
      the code being right and the fixture being wrong.

    Blocks of flat colour sit where a real screen sits. Measured at 1400x788: block=4 encodes
    to 399,492 characters, block=8 to 113,516, block=16 to 36,324; a real desktop capture was
    205,240. block=4 is the default here -- above the budget, and compressible enough that
    shrinking converges well above MIN_DIMENSION, which is the situation the budget is for.
    """
    import random

    from PIL import Image

    rnd = random.Random(1234)
    w, h = size
    small = Image.new("RGB", (max(1, w // block), max(1, h // block)))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                   for _ in range(small.size[0] * small.size[1])])
    p = tmp_path / name
    small.resize(size, Image.NEAREST).save(str(p), format="PNG")
    return p


@pytest.fixture
def anywhere(monkeypatch):
    """read_image validates against the server's allowed base; tmp_path is outside it."""
    from pathlib import Path

    monkeypatch.setattr(IO, "_validate_path", lambda s: Path(s))


def test_a_big_image_comes_back_within_the_budget_or_at_the_readability_floor(tmp_path,
                                                                              anywhere):
    """The contract, stated exactly: at most the budget, UNLESS meeting it would mean going
    below MIN_DIMENSION.

    Written as a single disjunction after the first two attempts each measured the fixture
    instead of the code. The synthetic block image is compressible at full size and stops
    being so once LANCZOS blurs the blocks into gradients, so it shrinks to the floor and is
    still over budget -- the loop doing exactly what it should. A real desktop capture goes
    the other way: 306,211 bytes on disk, 408,281 characters unbounded, 119,178 returned.
    Both are correct, and only the disjunction covers both without being tuned to either.
    """
    from io import BytesIO

    from PIL import Image

    p = _write(tmp_path, (1400, 788))
    out = IO.read_image(str(p))
    assert _is_image_result(out), "read_image returned text, not a picture: %r" % type(out)
    im = Image.open(BytesIO(_payload_bytes(out)))
    im.load()
    # THE DISJUNCTION IS CORRECT FOR THIS FIXTURE AND WAS NOT ENOUGH ON ITS OWN. Block noise
    # does not compress in ANY format once LANCZOS has blurred it, so the floor really is the
    # right answer here. But "within budget OR at the floor" is also satisfied by "at the
    # floor and 19% over budget", and that is what production did -- a 1600x900 capture
    # bottomed out at 400x225 and 142,912 characters, having thrown away 94% of the pixels and
    # still missed the ceiling, while this test passed every time. The strict property is
    # asserted below against content that behaves like a real screen.
    within = _payload_chars(out) <= IO.MAX_DATA_URI_CHARS + 200
    at_floor = max(im.size) <= IO.MIN_DIMENSION
    assert within or at_floor, (
        "returned %d characters at %dx%d -- over budget and not at the floor, so it stopped "
        "shrinking with room left" % (_payload_chars(out), im.size[0], im.size[1]))


def _gradient(tmp_path, size, name="screen.png"):
    """Content that behaves like a real screen: smooth ramps with local detail.

    This is the shape PNG handles badly and JPEG handles well -- anti-aliased text and window
    chrome on shaded backgrounds -- and it is the shape the production failure was measured
    on. Block noise (above) is the opposite and belongs to the floor case.
    """
    import random

    from PIL import Image

    rnd = random.Random(99)
    w, h = size
    im = Image.new("RGB", size)
    px = []
    for y in range(h):
        for x in range(w):
            base = (x * 255) // w
            shade = (y * 90) // h
            jitter = rnd.randrange(12)
            px.append((min(255, base + jitter), min(255, shade + jitter),
                       min(255, 200 - shade + jitter)))
    im.putdata(px)
    p = tmp_path / name
    im.save(str(p), format="PNG")
    return p


def test_a_screen_like_image_meets_the_budget_without_being_destroyed(tmp_path, anywhere):
    """THE PROPERTY THE PRODUCTION FAILURE VIOLATED, asserted strictly.

    Measured on the real 1600x900 capture that found it: as PNG it needed 400px to approach
    the budget and STILL missed at 142,912 characters; as JPEG it fits at 800px with 103,224.
    So the budget is met by changing format, not by shrinking until the picture is gone, and
    the returned image must be comfortably above the readability floor.
    """
    from io import BytesIO

    from PIL import Image

    p = _gradient(tmp_path, (1400, 800))
    unbounded = len(base64.b64encode(p.read_bytes()))
    assert unbounded > IO.MAX_DATA_URI_CHARS, (
        "this fixture does not exercise the budget: %d chars" % unbounded)

    out = IO.read_image(str(p))
    im = Image.open(BytesIO(_payload_bytes(out)))
    im.load()
    assert _payload_chars(out) <= IO.MAX_DATA_URI_CHARS + 200, (
        "returned %d characters at %dx%d" % (_payload_chars(out), im.size[0], im.size[1]))
    assert max(im.size) > IO.MIN_DIMENSION * 1.5, (
        "fitting the budget cost the picture: came back at %dx%d" % im.size)


def test_the_budget_binds_where_the_byte_cap_never_did(tmp_path, anywhere):
    """A file far under MAX_BYTES that is far over what a conversation can hold."""
    p = _write(tmp_path, (1400, 788))
    assert os.path.getsize(p) < IO.MAX_BYTES, "pick a bigger image; the byte cap would fire"
    unbounded = len(base64.b64encode(p.read_bytes()))
    assert unbounded > IO.MAX_DATA_URI_CHARS, (
        "this image does not exercise the budget: %d chars" % unbounded)
    assert _payload_chars(IO.read_image(str(p))) < unbounded


def test_a_small_image_is_returned_untouched(tmp_path, anywhere):
    """A cropped single window is the cheap path the guidance points at; it must not be
    degraded for nothing."""
    p = _write(tmp_path, (200, 120))
    out = IO.read_image(str(p))
    # BYTE-FOR-BYTE, which is what "untouched" means. Compared on the bytes rather than
    # on a base64 substring now that the result is an image block instead of a data URI
    # -- the question ("was it re-encoded?") and the answer are unchanged.
    assert _payload_bytes(out) == p.read_bytes(), \
        "a small image was re-encoded when it already fitted"


def test_what_comes_back_is_still_a_decodable_image(tmp_path, anywhere):
    """A budget met by returning something unreadable is not a saving."""
    from io import BytesIO

    from PIL import Image

    p = _write(tmp_path, (1400, 788))
    out = IO.read_image(str(p))
    payload = base64.b64encode(_payload_bytes(out)).decode("ascii")
    im = Image.open(BytesIO(base64.b64decode(payload)))
    im.load()
    assert max(im.size) >= IO.MIN_DIMENSION
    assert im.size[0] > im.size[1], "the aspect ratio was not preserved"


def test_it_stops_shrinking_rather_than_returning_something_unreadable(tmp_path, anywhere):
    """Per-pixel noise does not compress at ANY width, so no size satisfies a tiny budget.
    The floor must win: a 40x40 thumbnail of a screen is not worth the characters it saves."""
    from io import BytesIO

    from PIL import Image

    monkey = IO.MAX_DATA_URI_CHARS
    try:
        IO.MAX_DATA_URI_CHARS = 500
        p = _write(tmp_path, (1400, 788), block=1)
        out = IO.read_image(str(p))
        assert _is_image_result(out)
        im = Image.open(BytesIO(_payload_bytes(out)))
        im.load()
        assert max(im.size) >= IO.MIN_DIMENSION
    finally:
        IO.MAX_DATA_URI_CHARS = monkey


def test_the_helper_counts_base64_without_building_it(tmp_path):
    """Used on every call, so it must not cost an encode to ask the size."""
    for n in (0, 1, 2, 3, 4, 100, 999, 1000):
        assert IO._encoded_chars(b"x" * n) == len(base64.b64encode(b"x" * n)), n


def test_a_missing_pillow_returns_the_image_rather_than_an_error(tmp_path, anywhere,
                                                                 monkeypatch):
    """An optional dependency must not turn a working tool into a broken one; the resize
    branch above makes the same choice."""
    real = IO._fit_to_character_budget

    def _no_pil(data, suffix):
        import builtins

        orig = builtins.__import__

        def blocked(name, *a, **k):
            if name.startswith("PIL"):
                raise ImportError("no pillow")
            return orig(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", blocked)
        try:
            return real(data, suffix)
        finally:
            monkeypatch.setattr(builtins, "__import__", orig)

    monkeypatch.setattr(IO, "_fit_to_character_budget", _no_pil)
    p = _write(tmp_path, (1400, 788))
    out = IO.read_image(str(p))
    assert _is_image_result(out), "read_image returned text, not a picture: %r" % type(out)
