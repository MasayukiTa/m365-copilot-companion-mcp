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
    assert out.startswith("data:image/"), out[:120]
    im = Image.open(BytesIO(base64.b64decode(out.split(",", 1)[1])))
    im.load()
    within = len(out) <= IO.MAX_DATA_URI_CHARS + 200
    at_floor = max(im.size) <= IO.MIN_DIMENSION
    assert within or at_floor, (
        "returned %d characters at %dx%d -- over budget and not at the floor, so it stopped "
        "shrinking with room left" % (len(out), im.size[0], im.size[1]))


def test_the_budget_binds_where_the_byte_cap_never_did(tmp_path, anywhere):
    """A file far under MAX_BYTES that is far over what a conversation can hold."""
    p = _write(tmp_path, (1400, 788))
    assert os.path.getsize(p) < IO.MAX_BYTES, "pick a bigger image; the byte cap would fire"
    unbounded = len(base64.b64encode(p.read_bytes()))
    assert unbounded > IO.MAX_DATA_URI_CHARS, (
        "this image does not exercise the budget: %d chars" % unbounded)
    assert len(IO.read_image(str(p))) < unbounded


def test_a_small_image_is_returned_untouched(tmp_path, anywhere):
    """A cropped single window is the cheap path the guidance points at; it must not be
    degraded for nothing."""
    p = _write(tmp_path, (200, 120))
    out = IO.read_image(str(p))
    raw = base64.b64encode(p.read_bytes()).decode("ascii")
    assert raw in out, "a small image was re-encoded when it already fitted"


def test_what_comes_back_is_still_a_decodable_image(tmp_path, anywhere):
    """A budget met by returning something unreadable is not a saving."""
    from io import BytesIO

    from PIL import Image

    p = _write(tmp_path, (1400, 788))
    out = IO.read_image(str(p))
    payload = out.split(",", 1)[1]
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
        assert out.startswith("data:image/")
        im = Image.open(BytesIO(base64.b64decode(out.split(",", 1)[1])))
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
    assert out.startswith("data:image/"), out[:120]
