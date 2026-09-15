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

# use_last_error=True OR THE ERROR CODE IS NOISE. ctypes.windll caches a handle that does
# NOT preserve the Win32 last-error across the call, so ctypes.get_last_error() afterwards
# returns whatever happened to be there. The refusal message shipped on 2026-09-15 printed
# that value as "error %d" and it meant nothing.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.GetThreadDesktop.restype = wintypes.HANDLE
_user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
_user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
_user32.GetForegroundWindow.restype = wintypes.HWND

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

#: How long to leave between a modifier going down and the key that rides it. Measured
#: rather than chosen: at zero the characters typed into Notepad arrived and the Ctrl+A
#: that followed did not select them, twice. Small enough that a ten-key combination
#: costs a tenth of a second; large enough that an application which sets modifier state
#: on its message pump has seen it.
MODIFIER_SETTLE_S = 0.04

#: THE WHOLE KEYBOARD, BUILT RATHER THAN LISTED.
#:
#: This was a hand-picked set -- "the keys ordinary office work actually needs" -- which
#: sounds careful and is a hole. Win+M could not be pressed, because `m` was never a key I
#: happened to need; nor could Win+E, Win+Tab, Ctrl+B, or any of the hundreds of shortcuts
#: nobody had thought of yet. A caller cannot tell a key that is REFUSED from a key that
#: was simply never typed into this dict, and neither can a reader.
#:
#: Typing is not affected by any of it -- type_text sends characters through
#: KEYEVENTF_UNICODE and never reads this table -- so what a missing entry cost was always
#: a key COMBINATION, which is exactly how Windows is driven.
#:
#: What restrains this module is not an absent key. It is _REFUSED_COMBOS below, which
#: names the combinations that end the operator's session or their unsaved work. That is a
#: statement a reader can check. An incomplete table is a property nobody can see.
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "space": 0x20, "spacebar": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "menu": 0x12,
    "lctrl": 0xA2, "rctrl": 0xA3, "lshift": 0xA0, "rshift": 0xA1,
    "lalt": 0xA4, "ralt": 0xA5,
    # The Windows key. Left out at first because it is the one modifier that reaches the
    # shell rather than the focused application -- but leaving it out did not make the
    # desktop safe, it made opening an application impossible, since Win and Win+R are how
    # that is done.
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "contextmenu": 0x5D,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "break": 0x13,
    # Punctuation, named by the character printed on the key as well as by its OEM name: a
    # caller asking for "," should not have to know it is VK_OEM_COMMA. These are the codes
    # for a US layout -- on another layout the same code produces a different character,
    # which is why text goes through type_text and only SHORTCUTS come through here.
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "semicolon": 0xBA, "equals": 0xBB, "plus": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "dot": 0xBE, "slash": 0xBF, "backtick": 0xC0, "grave": 0xC0,
    "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD, "quote": 0xDE,
    # The numeric keypad, which is a different key from the digit row and is delivered
    # differently to applications that distinguish them.
    "numpad*": 0x6A, "numpadmultiply": 0x6A,
    "numpad+": 0x6B, "numpadadd": 0x6B,
    "numpad-": 0x6D, "numpadsubtract": 0x6D,
    "numpad.": 0x6E, "numpaddecimal": 0x6E,
    "numpad/": 0x6F, "numpaddivide": 0x6F,
    # Browser and media keys, present on most keyboards and the only way to reach some
    # hardware behaviour at all.
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplay": 0xB3,
    "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8,
    # Japanese IME keys. This machine runs a Japanese layout, where switching input mode is
    # part of typing rather than an exotic extra.
    "kanji": 0x19, "convert": 0x1C, "nonconvert": 0x1D, "kana": 0x15,
    "hankaku": 0xF3, "zenkaku": 0xF4, "hanzen": 0x19,
}
#: Letters, digits, the function row and the keypad digits, GENERATED so that none can be
#: forgotten. A hand-written list of twenty-six letters is twenty-six chances to miss one,
#: and the previous version missed thirteen.
VK.update({chr(c): 0x41 + c - ord("a") for c in range(ord("a"), ord("z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({"f%d" % n: 0x70 + n - 1 for n in range(1, 25)})
VK.update({"numpad%d" % d: 0x60 + d for d in range(10)})

#: Combinations that are refused however they are spelled. Not a security boundary -- a
#: caller holding this module can call _send directly -- but the difference between a
#: mistake that is possible and one that is easy. Each of these ends the operator's session
#: or their unsaved work, and none of them is ever part of doing their job.
_REFUSED_COMBOS = (
    frozenset({"win", "l"}),            # locks the workstation
    frozenset({"alt", "f4"}),           # closes whatever happens to be in front
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "shift", "escape"}),
)

#: Keys whose scan code needs the extended flag or they are delivered as the numpad
#: equivalent -- an arrow press that arrives as a numeric keypad digit is a classic
#: silent failure, visible only as text appearing where navigation was intended.
_EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2E,
             0x2D,                      # insert, the twin of numpad 0
             0x5C, 0x5D,                # right Windows key, context menu
             0xA3, 0xA5,                # right ctrl, right alt
             0x6F,                      # numpad divide
             0x90, 0x2C,                # numlock, print screen
             0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,   # media keys
             0xA6, 0xA7, 0xA8}          # browser keys


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


def input_desktop_name() -> str:
    """The name of the desktop that currently RECEIVES INPUT, or "" if it cannot be read.

    "Default" is an ordinary interactive session. "Winlogon" is the lock screen or a UAC
    prompt on the secure desktop, where nothing this process sends will arrive.

    A HANDLE IS NOT THE ANSWER, WHICH IS WHY THIS RETURNS A NAME. The first version of this
    check treated OpenInputDesktop succeeding as "not locked". Sampled on 2026-09-15 while
    the lock screen was the foreground window and GetForegroundWindow() returned 0, the call
    still handed back a handle -- so the handle says nothing and only the name does.
    """
    try:
        h = _user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return ""
        try:
            need = wintypes.DWORD()
            _user32.GetUserObjectInformationW(h, 2, None, 0, ctypes.byref(need))
            buf = ctypes.create_unicode_buffer(max(2, need.value))
            if not _user32.GetUserObjectInformationW(h, 2, buf, need.value,
                                                     ctypes.byref(need)):
                return ""
            return buf.value
        finally:
            _user32.CloseDesktop(h)
    except Exception:
        return ""


def thread_desktop_receives_input():
    """Is the desktop of the CALLING THREAD the one currently receiving user input?

    True / False / None, where None means the query itself failed and nothing may be
    concluded. This is the probe that answers the question the others only circle around,
    and it is asked on the same thread that calls SendInput, which is the thread whose
    desktop actually matters.

    WHY NOT input_desktop_name(). OpenInputDesktop succeeds and reports "Default" in a
    DISCONNECTED session too -- it names the desktop that will become active on
    reconnection, not the one receiving input now. So a "Default" reading is consistent with
    a session nobody is looking at, and the name alone cannot carry the conclusion. That was
    established after the code below was first written, which is why both are kept: the name
    is context, this is the answer.

    The handle from GetThreadDesktop is borrowed and must NOT be closed.
    """
    try:
        hdesk = _user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        if not hdesk:
            return None
        receiving = wintypes.BOOL(0)
        needed = wintypes.DWORD()
        # UOI_IO is 6. Written as 20 first and the call returned ERROR_INVALID_PARAMETER
        # (87) while UOI_NAME succeeded on the same handle, which is how the wrong
        # constant was found -- a sweep of 1,2,3,5,6,7 against a live desktop.
        ok = _user32.GetUserObjectInformationW(hdesk, 6,  # UOI_IO
                                               ctypes.byref(receiving),
                                               ctypes.sizeof(receiving),
                                               ctypes.byref(needed))
        if not ok:
            return None
        return bool(receiving.value)
    except Exception:
        return None


def _foreground_description() -> str:
    """Title and class of whatever is in front, or a note that there is nothing."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ("no foreground window at all, which happens on the lock screen and "
                    "during a UAC prompt")
        n = _user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        return "%r (class %s)" % (title.value or "(untitled)", cls.value)
    except Exception:
        return "unreadable"


def _send(*inputs: INPUT) -> int:
    """Send a batch atomically. Returns how many the OS accepted.

    A short count is not a detail to swallow: SendInput refuses silently when the input
    cannot reach its target, and a caller that ignored the count would report success for
    keystrokes nobody received.

    THE MESSAGE REPORTS, IT DOES NOT DIAGNOSE. It used to name one cause -- an elevated
    foreground window -- and on 2026-09-15 a worker believed it, spent six identical retries,
    and asked a human to "resolve the foreground window" while the thing in front was the
    lock screen, which runs BELOW this process rather than above it. A confident wrong cause
    is worse than none: it sent a person to fix something that was not broken.

    WHAT ASTRA CORRECTED, 2026-09-16. Neither the return count nor GetLastError identifies
    the mechanism -- Microsoft says so explicitly for UIPI, and there is no documented
    distinguishing value for the secure desktop or session isolation either. So no classifier
    of the form "0 + ACCESS_DENIED means secure desktop" is warranted, and none is built here.
    What IS decidable is narrower and sufficient: whether this thread's desktop is the one
    receiving user input. That single three-state fact separates "nobody can receive this"
    from "somebody could, but this window refuses it", which are the two different people who
    have to act.
    """
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    # CLEARED AND READ AROUND THE CALL, with nothing in between. Any other API call between
    # SendInput and the read replaces the value with its own.
    ctypes.set_last_error(0)
    sent = _user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    err = ctypes.get_last_error()
    if sent != n:
        receiving = thread_desktop_receives_input()
        raise InputRefused(
            "Windows accepted %d of %d inputs (last error %d). This thread's desktop is "
            "receiving user input: %s. Input desktop name: %s. In front: %s. "
            "Retrying unchanged will not help -- SendInput is refusing, not failing.%s"
            % (sent, n, err,
               {True: "yes", False: "NO", None: "could not be determined"}[receiving],
               input_desktop_name() or "unreadable", _foreground_description(),
               (" Since this thread's desktop is NOT the one receiving input, nothing sent "
                "from here reaches the user until that changes -- a locked session, the "
                "secure desktop during a UAC prompt, or a disconnected session. A person has "
                "to attend the machine." if receiving is False else
                " The desktop is receiving input, so the remaining documented cause is a "
                "foreground window at a higher integrity level than this process; bringing "
                "an ordinary window to the front is enough." if receiving is True else
                " With the desktop query itself failing, no cause can be named from here; "
                "report these values rather than guessing.")))
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
    names = [str(k).strip().lower() for k in keys]
    asked = frozenset(names)
    for combo in _REFUSED_COMBOS:
        if combo <= asked:
            raise InputRefused(
                "refusing %s: it ends the operator's session or their unsaved work, and is "
                "never part of doing their job. If that really is the intent, a person can "
                "press it." % "+".join(sorted(combo)))
    codes = []
    for name in names:
        vk = VK.get(name)
        if vk is None:
            # NAME THE NEAR MISSES, NOT THE WHOLE TABLE. Listing every known key was
            # readable when there were thirty of them; at 167 it is several hundred
            # characters of noise pushed into the caller's conversation for a typo, which
            # is the cost the tool index was just shrunk to avoid.
            near = sorted(k for k in VK if k.startswith(name[:2]) or name.startswith(k))
            raise InputRefused(
                "unknown key %r.%s Letters, digits, f1-f24, arrows, punctuation by the "
                "character on the key, numpad0-9 and the modifiers are all valid names."
                % (name, (" Did you mean: " + ", ".join(near[:8]) + "?") if near else ""))
        codes.append(vk)
    # SENT AS SEPARATE EVENTS WITH THE MODIFIER GIVEN TIME TO SETTLE, not as one batch.
    #
    # The first version put every down and every up into a single SendInput call, which
    # Windows accepts and delivers with no gap at all. Measured 2026-09-15: typing into
    # Notepad worked -- the characters were visibly in the window -- and the Ctrl+A,
    # Ctrl+C that followed copied nothing, twice, which the harness then reported as a
    # typing failure. The PowerShell helper this replaced had 40 ms between the modifier
    # and the key, and its comment did not say why; this is why.
    #
    # An application sees a modifier as a STATE it checks when the key arrives, and some
    # set that state on a message pump tick rather than synchronously. Zero gap means the
    # key can be processed before the modifier is considered held, so Ctrl+A arrives as a
    # bare 'a' -- which in a text box is not a no-op, it types a letter. Delivering them
    # apart costs single-digit milliseconds and removes a class of silent wrong action.
    for vk in codes:
        _send(*_key_inputs(vk))
        time.sleep(MODIFIER_SETTLE_S)
    for vk in reversed(codes):
        _send(*_key_inputs(vk, up=True))
        time.sleep(MODIFIER_SETTLE_S)


def release_all_modifiers() -> None:
    """Let go of ctrl/shift/alt whatever happened.

    Called after an interrupted or failed sequence. A modifier left down is the
    single nastiest residue this module can leave on a machine somebody else is
    using: every subsequent keystroke they type becomes a shortcut, and nothing on
    screen says why.
    """
    # EVERY MODIFIER THAT CAN BE PRESSED, not the three that used to be. The table now
    # names the left and right variants separately and the Windows key, so each of them can
    # be left down by an interrupted sequence -- and a stuck Win key turns every subsequent
    # letter the operator types into a shell shortcut. Releasing a key that was never down
    # costs nothing and is delivered as a no-op.
    batch = []
    for name in ("ctrl", "shift", "alt", "lctrl", "rctrl", "lshift", "rshift",
                 "lalt", "ralt", "lwin", "rwin"):
        batch.extend(_key_inputs(VK[name], up=True))
    try:
        _send(*batch)
    except InputRefused:
        pass
