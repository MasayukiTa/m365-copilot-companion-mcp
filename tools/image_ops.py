import base64
import mimetypes
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path

MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap for safety


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
