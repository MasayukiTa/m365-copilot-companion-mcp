# -*- coding: utf-8 -*-
"""Click, type, press and scroll on the real desktop -- and check it landed.

The executor half of computer use. Everything here takes coordinates in a
tools/screen_frame.Frame, because a number that came from looking at a picture is
meaningless without the frame it was read in, and because that frame is also the
record of what the desktop looked like at the moment the picture was taken.

THE FRAME IS CHECKED BEFORE EVERY ACTION, AND THAT IS THE WHOLE REASON IT IS
PASSED IN. A computer-use step is: capture, think, act. Time passes in the middle.
If a monitor is unplugged, plugged in, or rearranged during that gap -- and on this
operator's machine displays come and go -- the virtual desktop's origin and size
change, and the coordinate the model computed now names a different place, or no
place. Nothing in the coordinate itself can say so. So an action whose frame no
longer matches the desktop is REFUSED rather than performed on the new layout: a
click that goes somewhere unintended is worse than a click that does not happen,
and the caller can always capture again, which is cheap.

WHY SendInput AND NOT SetCursorPos + mouse_event. SendInput delivers the move and
the button as one atomic sequence that nothing can interleave with, and the older
calls are explicitly documented as superseded. Absolute coordinates are normalised
over the VIRTUAL desktop (MOUSEEVENTF_VIRTUALDESK), which is the only form that can
address a second monitor at all -- without that flag the coordinates are normalised
over the primary monitor and every click on any other screen lands on the primary.

WHY TYPING GOES THROUGH KEYEVENTF_UNICODE. Sending virtual key codes means sending
whatever the current keyboard layout maps them to, and it means fighting the IME
for anything Japanese. A unicode keystroke carries the character itself, so text
arrives as written regardless of layout or IME state. Characters outside the basic
plane are sent as their two surrogates, which is what the API expects.

WHAT THIS DOES NOT DO. It does not decide what to click. Nothing here looks at a
picture or calls a model; this is the hands, and it is deliberately separable so
that the executor can be calibrated on its own -- see scripts/calibrate_executor.py,
which measures the coordinate path with no model involved, and this module's
`where_did_it_go`, which measures the input path the same way.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Iterable, Optional, Tuple

from .screen_capture import make_process_dpi_aware, virtual_screen
from .screen_frame import Frame

_user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

#: The named keys a caller may press, as virtual key codes. Deliberately a small
#: fixed vocabulary rather than "any key name": a computer-use loop that can press
#: arbitrary keys can press Win+L, Alt+F4 and Ctrl+Alt+Del combinations by accident,
#: and the set below is what ordinary office work actually needs.
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "a": 0x41, "c": 0x43, "v": 0x56, "x": 0x58, "z": 0x5A, "s": 0x53, "f": 0x46,
}

#: Keys whose scan code needs the extended flag or they are delivered as the numpad
#: equivalent -- an arrow press that arrives as a numeric keypad digit is a classic
#: silent failure, visible only as text appearing where navigation was intended.
_EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2E}


class InputRefused(RuntimeError):
    """The action was not performed, and the message says what changed."""


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT


def _send(*inputs: INPUT) -> int:
    """Send a batch atomically. Returns how many the OS accepted.

    A short count is not a detail to swallow: SendInput refuses silently when the
    target window is at a higher integrity level than this process (an elevated
    app, the UAC dialog, the secure desktop), and a caller that ignored the count
    would report success for keystrokes nobody received.
    """
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = _user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise InputRefused(
            "Windows accepted %d of %d inputs (error %d). The usual cause is that the "
            "focused window runs at a higher integrity level than this process -- an "
            "elevated application, a UAC prompt, or the secure desktop -- which no "
            "amount of retrying will change."
            % (sent, n, ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0))
    return sent


def layout_matches(frame: Frame) -> bool:
    """Is the desktop still laid out the way it was when `frame` was captured?"""
    left, top, width, height = virtual_screen()
    return (left, top, width, height) == (frame.origin_x, frame.origin_y,
                                          frame.width, frame.height)


def _require_layout(frame: Frame) -> None:
    if not layout_matches(frame):
        left, top, width, height = virtual_screen()
        raise InputRefused(
            "the displays changed since this was seen: captured %dx%d at (%d, %d), "
            "now %dx%d at (%d, %d). The coordinate no longer names the place it was "
            "read from. Capture again rather than acting on the old one."
            % (frame.width, frame.height, frame.origin_x, frame.origin_y,
               width, height, left, top))


def _absolute(x: int, y: int) -> Tuple[int, int]:
    """Desktop pixel -> the 0..65535 grid SendInput uses across the virtual desktop.

    The denominator is (size - 1), not size. With `size` the last row and column
    are unreachable -- a click on the rightmost pixel of the rightmost monitor
    lands one pixel in, which is invisible on a window edge and fatal on a
    scrollbar or a close button.
    """
    left, top, width, height = virtual_screen()
    nx = int(round((x - left) * 65535.0 / max(1, width - 1)))
    ny = int(round((y - top) * 65535.0 / max(1, height - 1)))
    return (max(0, min(65535, nx)), max(0, min(65535, ny)))


def cursor_position() -> Tuple[int, int]:
    make_process_dpi_aware()
    p = POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _mouse(flags: int, x: Optional[int] = None, y: Optional[int] = None,
           data: int = 0) -> INPUT:
    mi = MOUSEINPUT(0, 0, data, flags, 0, None)
    if x is not None:
        nx, ny = _absolute(x, y)
        mi.dx, mi.dy = nx, ny
        mi.dwFlags = flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE
    return INPUT(INPUT_MOUSE, _UNION(mi=mi))


def move(frame: Frame, x: int, y: int) -> Tuple[int, int]:
    """Move the pointer to a desktop coordinate. Returns where it actually went.

    The return value is not decoration. The normalisation to SendInput's 0..65535
    grid is lossy on a desktop wider than 65535/1 pixels and, far more often, is
    where a DPI mistake shows up as a constant proportional offset. Reading the
    position back turns that from something inferred into something measured --
    see where_did_it_go.
    """
    _require_layout(frame)
    if not frame.contains_screen(x, y):
        raise InputRefused(
            "(%d, %d) is outside the captured desktop %dx%d at (%d, %d) -- nothing "
            "could have seen it there" % (x, y, frame.width, frame.height,
                                          frame.origin_x, frame.origin_y))
    _send(_mouse(0, x, y))
    return cursor_position()


def where_did_it_go(frame: Frame, points: Iterable[Tuple[int, int]],
                    settle_s: float = 0.01):
    """Move to each point and read back where the pointer landed. No clicks.

    The input half of the calibration: scripts/calibrate_executor.py proves a
    coordinate computed through the frame names the right element, and this proves
    the pointer can be put on that coordinate. Separate, because they fail for
    different reasons -- the first for a wrong origin or scale, the second for DPI
    virtualisation or the 0..65535 normalisation -- and a single combined number
    could not say which.

    Restores the pointer to where it started, because the operator is using this
    machine.
    """
    start = cursor_position()
    out = []
    try:
        for x, y in points:
            got = move(frame, x, y)
            if settle_s:
                time.sleep(settle_s)
                got = cursor_position()
            out.append(((x, y), got, max(abs(got[0] - x), abs(got[1] - y))))
    finally:
        try:
            _send(_mouse(0, start[0], start[1]))
        except Exception:
            pass
    return out


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(frame: Frame, x: int, y: int, button: str = "left",
          count: int = 1) -> Tuple[int, int]:
    """Move and click. Returns where the pointer was when the button went down.

    The move and the button press go in ONE SendInput batch so that nothing can
    move the pointer between them. Sent as two calls, a click can be delivered at a
    position the caller never chose -- rare, and correspondingly hard to reproduce
    when it happens.
    """
    _require_layout(frame)
    if button not in _BUTTONS:
        raise InputRefused("unknown button %r; expected one of %s"
                           % (button, ", ".join(sorted(_BUTTONS))))
    if not frame.contains_screen(x, y):
        raise InputRefused("(%d, %d) is outside the captured desktop" % (x, y))
    down, up = _BUTTONS[button]
    batch = [_mouse(0, x, y)]
    for _ in range(max(1, count)):
        batch.append(_mouse(down))
        batch.append(_mouse(up))
    _send(*batch)
    return cursor_position()


def double_click(frame: Frame, x: int, y: int, button: str = "left"):
    return click(frame, x, y, button=button, count=2)


def scroll(frame: Frame, x: int, y: int, clicks: int, horizontal: bool = False):
    """Scroll at a point. Positive is up (or right); one click is one notch."""
    _require_layout(frame)
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send(_mouse(0, x, y), _mouse(flag, data=int(clicks) * WHEEL_DELTA))
    return cursor_position()


def _unicode_pair(ch: str):
    code = ord(ch)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


def type_text(text: str, per_char_s: float = 0.0) -> int:
    """Type `text` as characters, not as keystrokes. Returns characters sent.

    Unaffected by the keyboard layout and by the IME, which is the point: the
    alternative is mapping each character to a virtual key for the layout that
    happens to be active, and then losing to the IME on anything Japanese.

    A newline in `text` is sent as Enter rather than as the character U+000A,
    because in every application that matters those do different things and the
    caller who wrote "\\n" meant the key.
    """
    made = []
    for ch in text:
        if ch == "\n":
            made.extend(_key_inputs(VK["enter"]))
            continue
        if ch == "\t":
            made.extend(_key_inputs(VK["tab"]))
            continue
        for unit in _unicode_pair(ch):
            ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, None)
            made.append(INPUT(INPUT_KEYBOARD, _UNION(ki=ki)))
            ki_up = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
            made.append(INPUT(INPUT_KEYBOARD, _UNION(ki=ki_up)))
    if not made:
        return 0
    if per_char_s:
        # Some applications drop characters delivered faster than they repaint.
        # Off by default because it is a workaround for a specific app, and a
        # caller should be able to see that it was needed rather than pay for it
        # everywhere.
        for i in range(0, len(made), 2):
            _send(*made[i:i + 2])
            time.sleep(per_char_s)
    else:
        _send(*made)
    return len(text)


def _key_inputs(vk: int, up: bool = False):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    ki = KEYBDINPUT(vk, 0, flags, 0, None)
    return [INPUT(INPUT_KEYBOARD, _UNION(ki=ki))]


def press(*keys: str) -> None:
    """Press a key, or a combination: press("ctrl", "s").

    Modifiers are held in the order given and released in reverse, which is what
    every application expects and what a naive implementation gets wrong by
    releasing in order -- leaving Ctrl down after the combination, so that the next
    ordinary keystroke arrives as a shortcut. That residue outlives the call, and
    the damage lands somewhere else entirely.
    """
    codes = []
    for k in keys:
        vk = VK.get(str(k).strip().lower())
        if vk is None:
            raise InputRefused(
                "unknown key %r. Known: %s" % (k, ", ".join(sorted(VK))))
        codes.append(vk)
    batch = []
    for vk in codes:
        batch.extend(_key_inputs(vk))
    for vk in reversed(codes):
        batch.extend(_key_inputs(vk, up=True))
    _send(*batch)


def release_all_modifiers() -> None:
    """Let go of ctrl/shift/alt whatever happened.

    Called after an interrupted or failed sequence. A modifier left down is the
    single nastiest residue this module can leave on a machine somebody else is
    using: every subsequent keystroke they type becomes a shortcut, and nothing on
    screen says why.
    """
    batch = []
    for name in ("ctrl", "shift", "alt"):
        batch.extend(_key_inputs(VK[name], up=True))
    try:
        _send(*batch)
    except InputRefused:
        pass
