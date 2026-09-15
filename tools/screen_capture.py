# -*- coding: utf-8 -*-
"""Capture the desktop together with the frame that says what its pixels mean.

An image on its own is not enough to click with. The number a model reads off a
screenshot is only a desktop coordinate after it has been put back through the
origin and scale the capture was taken at, and neither of those is visible in the
picture. So nothing here returns a bare image: every capture comes with its
tools/screen_frame.Frame, and the conversion lives there.

DPI AWARENESS IS NOT OPTIONAL AND MUST HAPPEN FIRST. A process that has not
declared itself per-monitor-DPI aware is lied to by Windows for its whole life:
GetSystemMetrics reports the virtual desktop in logical pixels while the bitmap
that comes back from a capture is physical, and GetCursorPos/SetCursorPos speak
logical too. On a 150%-scaled display that is a consistent 1.5x disagreement
between the frame we compute in and the surface we click on -- a clean
proportional error that looks exactly like a model whose coordinates drift the
further they get from the top-left corner. Worse, the declaration is latched by
the first call that needs it, so importing this module late, after something else
has already touched a window or a DC, silently leaves the process unaware.

Nothing here requires a package the fleet does not already have: Pillow for the
grab, ctypes for the rest. tools/screenshot_ops.py stays as it is -- it writes a
file for a person to look at, and downscales for that purpose. This module is for
the case where something is going to act on what it sees.
"""
from __future__ import annotations

import ctypes
from typing import Optional, Tuple

from .screen_frame import Frame, full_size

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2. V2 rather than V1 because V1 leaves
#: child windows and non-client areas scaled by the system, so a coordinate read off
#: a captured title bar or scrollbar is in a different frame from the client area
#: underneath it -- one process, two coordinate systems, no way to tell from a pixel.
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_awareness: Optional[str] = None


def make_process_dpi_aware() -> str:
    """Declare this process per-monitor-DPI aware. Idempotent; returns what was achieved.

    Falls back through the older APIs for the same reason the newest is tried first:
    a process that ends up merely system-DPI aware is still wrong on a second monitor
    at a different scale, but it is far less wrong than one that is unaware, and the
    returned string says which of those we got so a measurement can record it rather
    than assume it.
    """
    global _awareness
    if _awareness is not None:
        return _awareness
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2):
            _awareness = "per-monitor-v2"
            return _awareness
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2. Returns S_OK(0) or E_ACCESSDENIED when
        # awareness was already set -- already set is a success for our purposes.
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if hr in (0, -2147024891):
            _awareness = "per-monitor-v1"
            return _awareness
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            _awareness = "system"
            return _awareness
    except Exception:
        pass
    _awareness = "unaware"
    return _awareness


def virtual_screen() -> Tuple[int, int, int, int]:
    """(left, top, width, height) of the whole virtual desktop, in physical pixels.

    `left` and `top` are negative when a monitor sits above or to the left of the
    primary one. This is the single most common way a multi-monitor click lands on
    the wrong screen: treat the capture's (0, 0) as the desktop's (0, 0) and every
    coordinate is off by exactly one monitor's width.
    """
    make_process_dpi_aware()
    g = ctypes.windll.user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def capture(max_dimension: int = 0, region=None):
    """Grab the desktop, or one rectangle of it. Returns (PIL.Image, Frame).

    max_dimension=0 means full size, which is the default because a downscale is a
    loss of precision that the thing acting on the image cannot see. When a caller
    does downscale (to fit a model's input limit, say), the Frame records it, and
    screen_frame.round_trip_error will say how much precision that cost.

    REGION IS THE CHEAP AXIS, AND IT IS THE ONE THAT MATTERS IN A CONVERSATION.
    Measured 2026-09-15: workers whose task was to look at the screen hit the
    conversation's token limit in 9 of 11 runs, against 4 of 24 for workers that
    looked at nothing -- after two to seven turns. A picture costs far more in a
    conversation than its byte count suggests, and this machine's virtual desktop is
    3840x2173, so capturing all of it to answer "did that dialog open" spends the
    budget on three screens of wallpaper.

    `region` is (left, top, right, bottom) in DESKTOP coordinates -- the same frame
    tools/window_probe.py reports a window's rectangle in, so a caller can hand one
    straight through. The returned Frame describes the crop, so a pixel named in the
    cropped image still converts back to the right place on the desktop: the origin
    moves with the crop, which is exactly what the Frame is for.
    """
    from PIL import Image, ImageGrab

    make_process_dpi_aware()
    left, top, width, height = virtual_screen()
    if region is not None:
        r_left, r_top, r_right, r_bottom = (int(v) for v in region)
        # Clamped to what is actually on a screen: a window can hang off the edge,
        # and a crop that reaches past the desktop returns black pixels the model
        # would then be asked to find something in.
        r_left = max(left, min(r_left, left + width))
        r_top = max(top, min(r_top, top + height))
        r_right = max(r_left + 1, min(r_right, left + width))
        r_bottom = max(r_top + 1, min(r_bottom, top + height))
        left, top = r_left, r_top
        width, height = r_right - r_left, r_bottom - r_top
    try:
        img = ImageGrab.grab(bbox=(left, top, left + width, top + height),
                             all_screens=True)
    except TypeError:
        img = ImageGrab.grab()          # very old Pillow: primary monitor only

    if max_dimension and max(img.size) > max_dimension:
        ratio = max_dimension / float(max(img.size))
        img = img.resize((max(1, int(round(img.size[0] * ratio))),
                          max(1, int(round(img.size[1] * ratio)))), Image.LANCZOS)
        return img, Frame(left, top, width, height,
                          img.size[0], img.size[1]).validate()

    # The un-resized case says so in one word rather than leaving a reader to compare four
    # numbers and work out that two pairs are equal.
    return img, full_size(left, top, width, height)


def capture_reports_what_it_did() -> str:
    """One line describing the capture path, for a measurement to record verbatim.

    A grounding number measured on an unaware process and one measured on a
    per-monitor-v2 process are not the same measurement, and six months later
    nothing in a results file would say which was which.
    """
    left, top, width, height = virtual_screen()
    return ("dpi=%s virtual=%dx%d at (%d, %d)"
            % (make_process_dpi_aware(), width, height, left, top))
