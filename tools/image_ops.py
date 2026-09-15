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



def _encoded_chars(raw: bytes) -> int:
    """Characters a data URI will cost, without building it. base64 is 4 per 3 bytes."""
    return ((len(raw) + 2) // 3) * 4


def _fit_to_character_budget(data: bytes, suffix: str):
    """Downscale until the base64 fits MAX_DATA_URI_CHARS, or until further shrinking would
    make the image unreadable.

    Returns (bytes, suffix). Pillow absent means the budget cannot be enforced -- the caller
    gets the original rather than an error, which is the same choice the resize branch makes,
    because a missing optional dependency should not turn a working tool into a broken one.
    """
    if _encoded_chars(data) <= MAX_DATA_URI_CHARS:
        return data, suffix
    try:
        from io import BytesIO

        from PIL import Image as PILImage
    except ImportError:
        return data, suffix

    fmt = "PNG" if suffix == "png" else "JPEG"
    try:
        for _ in range(8):
            im = PILImage.open(BytesIO(data))
            im.load()
            w, h = im.size
            longest = max(w, h)
            if longest <= MIN_DIMENSION:
                return data, suffix
            # Aim straight at the budget rather than halving blindly: encoded size scales
            # roughly with AREA, so the edge ratio is the square root of the size ratio. The
            # 0.95 keeps a rounding miss from costing another whole round trip through PIL.
            ratio = (MAX_DATA_URI_CHARS / float(_encoded_chars(data))) ** 0.5 * 0.95
            target = max(MIN_DIMENSION, int(longest * ratio))
            if target >= longest:
                target = max(MIN_DIMENSION, int(longest * 0.85))
            scale = target / float(longest)
            out = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                            PILImage.LANCZOS)
            buf = BytesIO()
            kwargs = {"optimize": True}
            if fmt == "JPEG":
                kwargs["quality"] = 88
                out = out.convert("RGB")
            out.save(buf, format=fmt, **kwargs)
            data = buf.getvalue()
            suffix = "png" if fmt == "PNG" else "jpeg"
            if _encoded_chars(data) <= MAX_DATA_URI_CHARS:
                break
    except Exception:
        # A budget is a courtesy, not a guarantee worth failing a read over.
        return data, suffix
    return data, suffix


def read_image(path: str, max_dimension: Optional[int] = 1600) -> str:
    """Read an image file and return it as a data URI so a vision model can see it.

    Use this to verify a chart/diagram/screenshot was generated correctly before
    reporting completion. The returned string is `data:image/<type>;base64,...`
    and is directly consumable by vision-capable LLMs.

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
