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
        # WHAT THIS LINE USED TO LEAVE OUT. The reply said only the file's own size,
        # so nothing told a reader that a desktop wider than max_dimension had been
        # halved on the way here, nor that its top-left corner is not (0, 0) -- on
        # this machine the virtual desktop starts at (-985, -1093) because a monitor
        # sits above and to the left of the primary one. Coordinates read off this
        # image and used as desktop points are therefore wrong by more than the width
        # of most windows, and nothing in the old reply could have warned anyone.
        # Saying so is not a substitute for the frame: to CLICK on what was seen, use
        # screen_look, which records the frame and lets screen_click convert.
        note = ""
        try:
            from .screen_capture import virtual_screen

            left, top, vw, vh = virtual_screen()
            scale = vw / float(img.size[0]) if img.size[0] else 1.0
            note = (f" -- desktop is {vw}x{vh} at ({left}, {top})"
                    + (f", this image is 1:{scale:.3f} of it" if abs(scale - 1.0) > 1e-9
                       else "")
                    + ". To click on what you see here, take the picture with"
                      " screen_look instead: it records the frame.")
        except Exception:
            pass
        return (f"saved screenshot: {out} ({img.size[0]}x{img.size[1]} px, "
                f"{out.stat().st_size:,} bytes){note}")
    except Exception as e:
        # Same split as screen_ops: this call grabs the screen too, so a locked session
        # makes it fail for a reason that is not a defect in it.
        from .screen_ops import _unavailable_or_error
        return _unavailable_or_error("screenshot", e)
