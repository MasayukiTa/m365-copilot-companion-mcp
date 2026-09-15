# -*- coding: utf-8 -*-
"""Ask Windows what is at a point, and what rectangles are on screen, without clicking.

This is the oracle for calibrating a computer-use executor. The question a
calibration has to answer is narrow: when we compute a desktop coordinate for a
thing we already know the position of, does that coordinate actually land on that
thing? Answering it needs two facts from the OS and nothing from any model --
where things are, and what is at a point.

WHY HIT-TESTING AND NOT CLICKING. WindowFromPoint runs the same top-down hit test
that decides where a real mouse click is delivered, so agreement with it is
evidence about clicks. And it presses nothing. A calibration sweep over a few
hundred points on the operator's own desktop must not send a few hundred clicks
into whatever happens to be open; a measurement that damages the thing being
measured is not available to run often, and one that is not run often is one whose
result is always out of date.

WHY WIN32 WINDOWS AND NOT UI AUTOMATION. UIA would give finer elements, but it
needs comtypes, which this environment does not have, and the finer granularity is
not what is being measured here. This step separates COORDINATE-TRANSFORM error
from MODEL error; for that, any object whose rectangle the OS will state and whose
identity the OS will confirm at a point is sufficient. Element-level grounding is a
separate, later measurement, and mixing the two is how a transform bug gets
recorded as a model weakness.

WHAT OCCLUSION MEANS HERE. When the point we compute for a window's rectangle
returns some OTHER top-level window, the point is covered -- correct behaviour, and
not a transform error. That case is reported as its own outcome rather than folded
into failure, because a measurement that counts covered windows as misses reports a
number that moves with whatever the operator happened to leave open.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import List, NamedTuple, Optional

from .screen_capture import make_process_dpi_aware

_user32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_user32.WindowFromPoint.argtypes = [POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND

GA_ROOT = 2

_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class Target(NamedTuple):
    """A rectangle the OS says is on screen, and enough to recognise it again."""

    hwnd: int
    root: int
    left: int
    top: int
    right: int
    bottom: int
    cls: str
    title: str

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def centre(self):
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def label(self) -> str:
        name = (self.title or "").strip()
        return "%s%s %dx%d" % (self.cls, (" '%s'" % name[:28]) if name else "",
                               self.width, self.height)


def _text(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _cls(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd: int) -> Optional[RECT]:
    r = RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r


def window_at(x: int, y: int) -> Optional[Target]:
    """What Windows would deliver a click at (x, y) to. None when nothing is there."""
    make_process_dpi_aware()
    hwnd = _user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return None
    return describe(int(hwnd))


def describe(hwnd: int) -> Optional[Target]:
    r = _rect(hwnd)
    if r is None:
        return None
    root = _user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd
    return Target(hwnd, int(root), r.left, r.top, r.right, r.bottom,
                  _cls(hwnd), _text(hwnd))


def foreground_window() -> Optional[Target]:
    make_process_dpi_aware()
    hwnd = _user32.GetForegroundWindow()
    return describe(int(hwnd)) if hwnd else None


def children_of(hwnd: int, min_side: int = 8) -> List[Target]:
    """Visible child windows of `hwnd` with a rectangle worth aiming at.

    min_side drops slivers: a 2-pixel-wide separator is a real window, but a
    calibration that spends its probes on separators measures how well we can hit
    things nobody clicks. It is a parameter rather than a constant so that a later
    run can deliberately measure the small ones, which is where a half-pixel of
    transform error first becomes a miss.
    """
    make_process_dpi_aware()
    found: List[Target] = []

    def _cb(child, _lparam):
        try:
            if _user32.IsWindowVisible(child):
                t = describe(int(child))
                if t and t.width >= min_side and t.height >= min_side:
                    found.append(t)
        except Exception:
            pass
        return True

    _user32.EnumChildWindows(wintypes.HWND(hwnd), _ENUMPROC(_cb), 0)
    return found


def top_level_windows(min_side: int = 64) -> List[Target]:
    """Visible top-level windows, front to back is NOT guaranteed -- see below.

    EnumWindows walks in z-order, topmost first, which is why the calibration uses
    the order it gets rather than sorting: the first window to contain a point is
    the one that would receive the click, and reproducing that ordering is part of
    what is being checked.
    """
    make_process_dpi_aware()
    found: List[Target] = []

    def _cb(hwnd, _lparam):
        try:
            if _user32.IsWindowVisible(hwnd):
                t = describe(int(hwnd))
                if t and t.width >= min_side and t.height >= min_side and t.title:
                    found.append(t)
        except Exception:
            pass
        return True

    _user32.EnumWindows(_ENUMPROC(_cb), 0)
    return found
