# -*- coding: utf-8 -*-
"""End to end: open a window, click into it, type, and read back what arrived.

Measurement 1 (scripts/calibrate_executor.py) proves a coordinate computed through
the frame names the right element. That is the coordinate path. This proves the
INPUT path -- that the pointer can be put on such a coordinate, that a button press
is delivered there, and that characters typed afterwards arrive as written.

They are separate runs because they fail for different reasons. A wrong origin or
scale breaks the first; DPI virtualisation, the 0..65535 normalisation, an integrity
level this process cannot reach, or an IME eating the keystrokes breaks the second.
A single end-to-end number could not say which, and "computer use does not work" is
not a finding anyone can act on.

IT USES ITS OWN WINDOW. Typing into whatever the operator left open is not a test,
it is damage. This launches its own Notepad, works inside it, reads the text back,
and kills the process it started -- never any other. The clipboard is borrowed for
the read-back and put back as it was, because the operator is using this machine and
a silently replaced clipboard is the kind of thing that is noticed an hour later
while pasting something else.

The Japanese in the sample is deliberate: it is the case that fails when text is
sent as virtual key codes instead of as characters, and this office's work is
Japanese, so an ASCII-only pass would prove nothing about the work.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import desktop_input as DI
from tools import win_hit_test as W
from tools.screen_capture import capture, capture_reports_what_it_did

SAMPLE = "computer-use 実行器の確認 ABC 123 釜の蓋"

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
CF_UNICODETEXT = 13

# ctypes defaults every return type to a 32-bit int, which silently truncates a
# 64-bit handle or pointer to its low half. The result is not an error, it is a
# plausible-looking number that faults when dereferenced -- as it did here, with an
# access violation inside wstring_at rather than anything naming the cause. Declare
# them.
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE


def clipboard_text():
    if not _user32.OpenClipboard(None):
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text):
    """Put `text` back on the clipboard. Used only to restore what we borrowed."""
    if text is None:
        return
    if not _user32.OpenClipboard(None):
        return
    try:
        _user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h = _kernel32.GlobalAlloc(0x0042, size)         # GMEM_MOVEABLE|GMEM_ZEROINIT
        p = _kernel32.GlobalLock(h)
        ctypes.memmove(p, buf, size)
        _kernel32.GlobalUnlock(h)
        _user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        _user32.CloseClipboard()


def pid_of(hwnd):
    owner = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
    return owner.value


def _new_window_after(before_hwnds, deadline_s=12.0):
    """Wait for a visible top-level window that did not exist before we launched.

    NOT matched by the pid we spawned. Notepad on Windows 11 is a packaged app:
    `notepad.exe` is a stub that hands off to the real application under a
    different process, so the pid subprocess.Popen returns owns no window at all,
    and matching on it finds nothing while the window is plainly on screen. What is
    reliable is that a window appeared which was not there a moment ago.
    """
    end = time.time() + deadline_s
    while time.time() < end:
        for t in W.top_level_windows(min_side=120):
            if t.hwnd not in before_hwnds:
                return t
        time.sleep(0.25)
    return None


def main():
    print(capture_reports_what_it_did())
    saved_clipboard = clipboard_text()
    proc = None
    window_pid = None
    ok = True
    try:
        before = {t.hwnd for t in W.top_level_windows(min_side=120)}
        proc = subprocess.Popen(["notepad.exe"])
        win = _new_window_after(before)
        if win is None:
            print("FAIL: notepad's window never appeared; nothing was measured.")
            return 2
        window_pid = pid_of(win.hwnd)
        print("launched notepad; its window belongs to pid=%d (the stub we spawned "
              "was pid=%d)" % (window_pid, proc.pid))
        print("window: %s at (%d, %d) %dx%d"
              % (win.label(), win.left, win.top, win.width, win.height))

        _user32.SetForegroundWindow(wintypes.HWND(win.hwnd))
        time.sleep(0.4)

        img, frame = capture()
        print(frame.describe())

        # Aim well inside the text area: below the title bar and any tab strip,
        # and in from the left margin. Not the centre -- a centred point in an
        # empty document is still the text area, but on a window that opened with
        # a dialog in front of it the centre is the dialog, and this must fail
        # loudly rather than type into whatever that is.
        x = win.left + win.width // 3
        y = win.top + win.height // 2

        under = W.window_at(x, y)
        if under is None or under.root != win.hwnd:
            print("FAIL: (%d, %d) is not notepad -- it is %s. Refusing to type there."
                  % (x, y, under.label() if under else "nothing"))
            return 2
        print("hit test at (%d, %d): %s  -- it is our window" % (x, y, under.label()))

        landed = DI.click(frame, x, y)
        print("clicked; pointer at %s (asked for (%d, %d), off by %d px)"
              % (landed, x, y, max(abs(landed[0] - x), abs(landed[1] - y))))

        n = DI.type_text(SAMPLE)
        print("typed %d characters" % n)
        time.sleep(0.3)

        DI.press("ctrl", "a")
        DI.press("ctrl", "c")
        time.sleep(0.4)
        got = clipboard_text() or ""
        got = got.replace("\r\n", "\n").strip()

        print()
        print("  wanted: %r" % SAMPLE)
        print("  got   : %r" % got)
        if got == SAMPLE:
            print("\nINPUT PATH SOUND: the click landed in the window we chose and every "
                  "character arrived as written, Japanese included.")
        else:
            ok = False
            print("\nINPUT PATH NOT SOUND: what arrived is not what was sent.")
            if not got:
                print("  nothing came back at all -- either the click did not focus the "
                      "text area, or the keystrokes were not delivered to this process.")
            elif SAMPLE.replace(" ", "") == got.replace(" ", ""):
                print("  only spacing differs.")
            else:
                for i, (a, b) in enumerate(zip(SAMPLE, got)):
                    if a != b:
                        print("  first difference at %d: sent %r, got %r" % (i, a, b))
                        break
    except DI.InputRefused as e:
        ok = False
        print("\nREFUSED: %s" % e)
    finally:
        DI.release_all_modifiers()
        # Only the window this script caused to exist, found by its handle rather
        # than by a name -- killing "every notepad" would take the operator's own
        # notes with it. Killed rather than closed because Notepad would put a
        # save prompt on their desktop, which is worse than a scratch document
        # that never existed.
        for pid in {p for p in (window_pid, proc.pid if proc is not None else None) if p}:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except Exception as e:
                print("\ncould not close pid=%d: %s" % (pid, e))
        print("\nclosed the notepad this run opened")
        set_clipboard_text(saved_clipboard)
        print("clipboard restored (%s)"
              % ("was empty" if not saved_clipboard else "%d chars" % len(saved_clipboard)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
