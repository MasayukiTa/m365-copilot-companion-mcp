import time
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked


def screenshot(
    output_path: Optional[str] = None,
    region: Optional[list[int]] = None,
    max_dimension: int = 1920,
) -> str:
    """Capture the screen (or a region) to a PNG file and return its path.

    Pair with read_image to let the agent inspect what is on screen, debug a
    GUI flow, or document what the user is looking at right now.

    Args:
        output_path: Where to save. If omitted, writes to
            ~/Desktop/screenshots/<timestamp>.png under the allowed base.
        region: Optional [left, top, right, bottom] in pixels. Omit for full screen.
        max_dimension: Downscale longest edge to this many pixels (saves space).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            return "[screenshot error: Pillow not installed]"

        if output_path is None:
            ts = time.strftime("%Y%m%d-%H%M%S")
            default_dir = Path.home() / "Desktop" / "screenshots"
            output_path = str(default_dir / f"{ts}.png")

        out = _validate_path(output_path)
        if out.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            return "[screenshot error: output_path must be .png / .jpg / .jpeg]"
        out.parent.mkdir(parents=True, exist_ok=True)

        bbox = None
        if region:
            if len(region) != 4:
                return "[screenshot error: region must be [left, top, right, bottom]]"
            bbox = tuple(int(v) for v in region)

        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            # Older Pillow without all_screens kwarg
            img = ImageGrab.grab(bbox=bbox)

        w, h = img.size
        longest = max(w, h)
        if longest > max_dimension:
            ratio = max_dimension / longest
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        save_kwargs = {"optimize": True}
        if out.suffix.lower() in {".jpg", ".jpeg"}:
            img = img.convert("RGB")
            save_kwargs["quality"] = 88
        img.save(out, **save_kwargs)
        return f"saved screenshot: {out} ({img.size[0]}x{img.size[1]} px, {out.stat().st_size:,} bytes)"
    except Exception as e:
        return f"[screenshot error: {type(e).__name__}: {e}]"
