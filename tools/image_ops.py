import base64
import mimetypes
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path

MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap for safety -- on the FILE, which is not what breaks

#: THE CEILING THAT ACTUALLY BINDS, in the currency the caller spends.
#:
#: MAX_BYTES above has never once been the binding constraint. It caps the file at 8 MB, which
#: is about 10.7 MILLION characters of base64 -- and what runs out is the conversation, not the
#: process. Measured over 424 read_image calls in .fleet/tool_events.jsonl: median 142,642
#: characters returned, p90 1,166,294, max 1,762,326. One call could put over a million and a
#: half characters into a chat and the cap was satisfied.
#:
#: The downscale below made it worse by looking harmless: it only ran when the image was
#: LARGER than max_dimension, so a 1400x788 desktop capture at the 1600 default was encoded
#: untouched. Measured on a real capture -- 1400x788 unresized is 205,240 characters; 1200 is
#: 176,460; 1024 is 141,724; 900 is 113,996; 800 is 94,512.
#:
#: 120,000 lands a full desktop at roughly 900px, which is legible, and leaves a cropped
#: single window -- the thing screen_look's `window=` argument produces, about seven times
#: smaller -- untouched. It is a CEILING, so the p90 and the max above stop being reachable
#: at all; that is the property, not the exact figure.
#:
#: This does not on its own fix the loop it was found in. Seventeen calls in one run at the
#: median is 2,424,914 characters; seventeen at this ceiling is still 2,040,000. The count is
#: the larger lever, which is why screen_look grew a window crop and why screen_windows --
#: median 506 characters, 282 times cheaper -- answers "what is open" without a picture.
MAX_DATA_URI_CHARS = 120_000

#: Stop shrinking here. Below this a screenshot stops being readable, and returning something
#: unreadable to save characters is not a saving.
MIN_DIMENSION = 400

#: JPEG quality for the fallback. 85 keeps screen text legible -- the content this path
#: actually meets -- while being the difference between 470,444 characters and 103,224
#: for one 800px capture. Lower starts ringing around glyphs, which is the one thing a
#: screenshot is read for.
JPEG_QUALITY = 85



def _encoded_chars(raw: bytes) -> int:
    """Characters a data URI will cost, without building it. base64 is 4 per 3 bytes."""
    return ((len(raw) + 2) // 3) * 4


def _fit_to_character_budget(data: bytes, suffix: str):
    """Bring the encoded size under MAX_DATA_URI_CHARS, losing as little of the picture as
    possible. Returns (bytes, suffix).

    WHY THE FIRST VERSION FAILED, and it failed in production rather than in a test. It only
    ever shrank, keeping PNG, and PNG is the wrong format for a screenshot once LANCZOS has
    blurred its flat runs into gradients. Measured on a real 1600x900 capture:

        1600x900  1,492,444 chars   (PNG, untouched)
         430x242    157,752         (PNG, aiming at the budget and overshooting)
         400x225    142,912         (PNG, MIN_DIMENSION reached -- stop)

    So it bottomed out at the readability floor, still 19% over the ceiling, having thrown
    away 94% of the pixels on the way. Both constants were satisfied and the result was the
    worst of both: over budget AND unreadable. The unit test passed throughout, because it
    asserts "within budget OR at the floor" -- a disjunction that is true here and says
    nothing useful. A live run reading one 1.1 MB file is what found it.

    The same image as JPEG:

         800x450    103,224 chars   (q85) -- inside the budget
        1024x576    153,880
         400x225     31,288

    So the answer is not fewer pixels, it is a format that suits the content. At 800px this
    returns four times the picture AND fits, where the old path returned a quarter of it and
    did not.

    PNG IS STILL TRIED FIRST and kept whenever it fits, because it is exact: a diagram, a
    chart or a screenshot of code survives it without ringing around the glyphs. JPEG is the
    fallback for the case where exactness was going to be lost anyway.
    """
    if _encoded_chars(data) <= MAX_DATA_URI_CHARS:
        return data, suffix
    try:
        from io import BytesIO

        from PIL import Image as PILImage
    except ImportError:
        return data, suffix

    try:
        im = PILImage.open(BytesIO(data))
        im.load()
        w, h = im.size
        longest = max(w, h)
        rgb = None

        def _at(dim, fmt):
            """Encode at this longest-edge, or None if it does not fit the budget."""
            scale = 1.0 if dim >= longest else dim / float(longest)
            src = im if fmt == "PNG" else (rgb or im.convert("RGB"))
            out = src if scale >= 1 else src.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
            buf = BytesIO()
            if fmt == "PNG":
                out.save(buf, format="PNG", optimize=True)
            else:
                out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            raw = buf.getvalue()
            if _encoded_chars(raw) > MAX_DATA_URI_CHARS:
                return None
            return (out.size[0] * out.size[1], raw, "png" if fmt == "PNG" else "jpeg")

        rgb = im.convert("RGB")

        # THE OBJECTIVE IS PIXELS, NOT THE FIRST THING THAT FITS. The previous rule shrank as
        # PNG and returned as soon as it was under budget, which on a real 816 KB capture gave
        # 425x238 -- inside the budget and barely readable, when JPEG at 1024px also fits.
        # Measured on one 1600x900 screenshot: PNG needs 400px to approach the budget and
        # still misses it at 142,912 characters, while JPEG fits at 800px with 103,224.
        #
        # So every candidate is priced and the biggest survivor wins. PNG is preferred only on
        # a tie, where it is free exactness: a diagram or a screenshot of code keeps its edges.
        # ENCODED SIZE FALLS WITH DIMENSION, so walking DOWN and stopping at the first fit
        # gives that format's largest survivor -- there is no need to price the rest. Pricing
        # all nine widths in both formats was the first version and cost 1.2 to 3.1 SECONDS
        # per call, measured on these captures; read_image is called hundreds of times, so
        # that lands inside every worker's turn.
        best = None
        dims = [d for d in (longest, 1600, 1280, 1024, 900, 800, 640, 512, MIN_DIMENSION)
                if MIN_DIMENSION <= d <= longest]
        # PNG GETS TWO PROBES, NOT NINE. When PNG cannot fit at any useful size -- the normal
        # case for a photographic or anti-aliased screen capture -- walking all nine widths
        # meant nine expensive PNG encodes that were all going to fail, and that alone was the
        # 1.2-3.1 seconds. Encoded size tracks AREA, so the width that could fit is
        # longest * sqrt(budget / chars_at_full); if that lands below the readability floor,
        # PNG is hopeless and is skipped entirely. The estimate is optimistic for PNG (its
        # compression degrades as resizing blurs flat runs), which is fine: an optimistic
        # probe that fails costs one encode, and JPEG is right behind it.
        png_dims = []
        if (suffix or "").lower() == "png":
            full = _encoded_chars(data)
            est = int(longest * ((MAX_DATA_URI_CHARS / float(full)) ** 0.5)) if full else 0
            png_dims = [d for d in dict.fromkeys([longest, est]) if MIN_DIMENSION <= d <= longest]
        for fmt, cand in (("PNG", png_dims), ("JPEG", dims)):
            for dim in cand:
                got = _at(dim, fmt)
                if got:
                    if best is None or got[0] > best[0] or (got[0] == best[0]
                                                            and got[2] == "png"):
                        best = got
                    break       # descending: this is the biggest that fits for this format
        if best:
            return best[1], best[2]

        # Nothing fits even at the floor. Return the smallest readable JPEG rather than the
        # original: over budget is bad, over budget AND huge is worse.
        scale = MIN_DIMENSION / float(longest)
        out = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "jpeg"
    except Exception:
        # A budget is a courtesy, not a guarantee worth failing a read over.
        return data, suffix


def read_image(path: str, max_dimension: Optional[int] = 1600) -> str:
    """Read an image file and return it as a base64 data URI -- TEXT, not a picture.

    NOTHING IN THIS STACK SEES THE RESULT. The return type is `str`, and FastMCP serialises a
    str as a text content block; no client here renders that as an image. Measured 2026-09-17:
    a worker asked to read six characters off a picture called this tool and then answered a
    string that was not on it, twice, describing both times how it had looked. The refuter
    caught it both times.

    So do not use this to check what a picture LOOKS like. Use instead:

      * `ocr_image(path)` for text in the picture -- measured on the same file, it returned the
        six characters exactly.
      * `run_python` with PIL/numpy for pixel facts -- dimensions, a colour at a point, whether
        a region is blank. Deterministic, and cheap.
      * `ANALYZE: <absolute path> | <instruction>` as the last line of your turn, to put the
        file in front of Copilot itself through a real file attachment. That is the only path
        in this repository that shows a picture to a model.

    THE COST IS NOT SMALL. Median 142,642 characters per call over 424 calls.

    Args:
        path: Image path (.png, .jpg, .jpeg, .gif, .bmp, .webp).
        max_dimension: If set and Pillow is available, downscale longest edge to
            this many pixels before encoding to keep payload small.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[read_image error: not a file: {p}]"
        suffix = p.suffix.lower().lstrip(".")
        if suffix not in {"png", "jpg", "jpeg", "gif", "bmp", "webp"}:
            return f"[read_image error: unsupported format: .{suffix}]"

        data = p.read_bytes()
        if max_dimension:
            try:
                from io import BytesIO

                from PIL import Image as PILImage

                im = PILImage.open(BytesIO(data))
                im.load()
                w, h = im.size
                longest = max(w, h)
                if longest > max_dimension:
                    ratio = max_dimension / longest
                    new_size = (int(w * ratio), int(h * ratio))
                    im = im.resize(new_size, PILImage.LANCZOS)
                    buf = BytesIO()
                    fmt = "PNG" if suffix == "png" else "JPEG"
                    save_kwargs = {"optimize": True}
                    if fmt == "JPEG":
                        save_kwargs["quality"] = 88
                        im = im.convert("RGB")
                    im.save(buf, format=fmt, **save_kwargs)
                    data = buf.getvalue()
                    suffix = "png" if fmt == "PNG" else "jpeg"
            except ImportError:
                pass

        # SHRINK UNTIL IT FITS, rather than only when it started too big. The branch above
        # resizes when the image exceeds max_dimension; this one asks the question that
        # matters -- how much of the conversation will this cost -- and keeps halving the
        # longest edge until the answer is affordable or the image would stop being readable.
        # Silent by design: the return value is a data URI and a note appended to it would
        # corrupt the thing the caller is about to decode.
        data, suffix = _fit_to_character_budget(data, suffix)

        if len(data) > MAX_BYTES:
            return (
                f"[read_image error: image is {len(data):,} bytes after resize; "
                f"limit {MAX_BYTES:,}. Lower max_dimension.]"
            )
        mime = mimetypes.guess_type(f"f.{suffix}")[0] or f"image/{suffix}"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        return f"[read_image error: {type(e).__name__}: {e}]"


def image_info(path: str) -> str:
    """Return size, format, and mode of an image file without loading full pixels.

    Use this for a quick sanity check that an image file exists and has a
    plausible size, when a base64 payload would be wasteful.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[image_info error: not a file: {p}]"
        try:
            from PIL import Image as PILImage

            with PILImage.open(p) as im:
                im.load()
                return (
                    f"path: {p}\n"
                    f"format: {im.format}\n"
                    f"mode: {im.mode}\n"
                    f"size: {im.size[0]} x {im.size[1]} px\n"
                    f"bytes: {p.stat().st_size:,}"
                )
        except ImportError:
            return f"path: {p}\nbytes: {p.stat().st_size:,}\n(Pillow not installed; size unknown)"
    except Exception as e:
        return f"[image_info error: {type(e).__name__}: {e}]"
