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
from tools import window_probe as W
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


#: What an untouched document is called, by locale. Notepad puts the document name in the
#: caption followed by " - <app name>", and marks an edited one with a leading asterisk.
#:
#: THIS LIST WAS WRONG ON ITS FIRST OUTING and refused a document that was in fact empty:
#: it knew 無題 and Untitled and not タイトルなし, which is what this machine says. A guard
#: that rejects the good case is a guard that gets switched off, so the names are data and
#: the comparison strips the application suffix rather than matching the whole caption.
_EMPTY_DOCUMENT_NAMES = ("", "タイトルなし", "無題", "Untitled", "新規", "New")


def _is_empty_document(caption) -> bool:
    """True when this caption describes a document nobody has typed into yet."""
    text = (caption or "").strip().lstrip("*").strip()
    # " - メモ帳" / " - Notepad": the document name is everything before the last dash.
    if " - " in text:
        text = text.rsplit(" - ", 1)[0].strip()
    return text in _EMPTY_DOCUMENT_NAMES


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
        # START FROM AN EMPTY DOCUMENT. Notepad on Windows 11 restores the previous
        # session, so a fresh window can open holding the text of the last run -- three
        # runs' worth had accumulated before this was noticed, and any comparison against
        # what we sent would have been against that pile rather than against our typing.
        # Ctrl+N gives a new tab; the check below is what makes it a fact rather than a
        # hope.
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

        # BEFORE PRESSING ANYTHING: can the pointer be PUT where we ask? Moving and reading
        # the position back separates the two ways this half fails -- DPI virtualisation and
        # SendInput's 0..65535 normalisation land here as a displacement, while a button that
        # never arrives lands later as text that does not appear. One number could not say
        # which. No clicks, and the pointer is put back where the operator left it.
        sweep = [(win.left + win.width * a // 8, win.top + win.height * b // 8)
                 for a in (1, 4, 7) for b in (1, 4, 7)]
        landings = DI.where_did_it_go(frame, sweep)
        worst = max((off for _asked, _got, off in landings), default=0)
        print("pointer sweep: %d points, worst displacement %d px" % (len(landings), worst))
        if worst:
            for asked, got, off in landings:
                if off:
                    print("   asked %s -> landed %s (off by %d)" % (asked, got, off))

        landed = DI.click(frame, x, y)
        # A restored session shows up in the caption. Ask for a new document and confirm
        # the caption stops carrying someone else's text before typing anything.
        restored = W.describe(win.hwnd)
        if restored and not _is_empty_document(restored.title):
            print("the window opened holding a restored session (%r) -- asking for a new one"
                  % (restored.title or "")[:40])
            DI.press("ctrl", "n")
            time.sleep(0.9)
            fresh_win = _new_window_after(before | {win.hwnd}, 6.0) or win
            win = fresh_win
            again = W.describe(win.hwnd)
            if again and not _is_empty_document(again.title):
                print("NOT MEASURING: could not get an empty document (%r). A comparison "
                      "against a document that already had text in it would be meaningless."
                      % (again.title or "")[:40])
                return 5
            x = win.left + win.width // 3
            y = win.top + win.height // 2
            DI.click(frame, x, y)
            time.sleep(0.3)
        print("clicked; pointer at %s (asked for (%d, %d), off by %d px)"
              % (landed, x, y, max(abs(landed[0] - x), abs(landed[1] - y))))

        # WHOSE KEYBOARD IS THIS. type_text goes wherever focus is, so a result of "nothing
        # arrived" has two causes that look identical: the keystrokes were refused, or they
        # went somewhere else because the operator is using this machine. This harness shares
        # a desktop with a person, and a measurement that cannot say "I was disturbed"
        # reports their typing as our defect. Checked immediately before and after, because
        # focus can move during the typing as well as before it.
        fg_before = W.foreground_window()
        if fg_before is None or fg_before.root != win.hwnd:
            print("DISTURBED: focus is on %s, not the window we opened. Not a result about "
                  "typing -- somebody else is using this desktop."
                  % (fg_before.label()[:44] if fg_before else "nothing"))
            return 3

        n = DI.type_text(SAMPLE)
        print("typed %d characters" % n)
        time.sleep(0.3)

        fg_after = W.foreground_window()
        if fg_after is None or fg_after.root != win.hwnd:
            print("DISTURBED: focus moved to %s while typing. Not a result about typing."
                  % (fg_after.label()[:44] if fg_after else "nothing"))
            return 3

        # TWO WITNESSES, BECAUSE ONE OF THEM IS NOT ABOUT TYPING. The clipboard needs
        # Ctrl+A, Ctrl+C, the application's own copy handler and the clipboard itself all
        # to work; when any of those fails it returns "" -- which is indistinguishable
        # from the characters never arriving. This harness reported INPUT PATH NOT SOUND
        # on exactly that, twice, while a picture of the window showed the text sitting
        # in it. A measurement that cannot tell "it did not arrive" from "I cannot read
        # it" will eventually accuse the thing it is measuring.
        #
        # The window's own title is the second witness: Notepad puts the first line of
        # the document in it, and reading a top-level window's caption needs nothing
        # from the application.
        DI.press("ctrl", "a")
        DI.press("ctrl", "c")
        time.sleep(0.4)
        got = (clipboard_text() or "").replace("\r\n", "\n").strip()

        caption = ""
        fresh = W.describe(win.hwnd)
        if fresh:
            # Notepad marks an edited document with a leading '*' and truncates the rest.
            caption = (fresh.title or "").lstrip("*").strip()
        head = SAMPLE[:max(1, len(caption))] if caption else ""
        if not got and caption and caption.startswith(head[:8]):
            print("the clipboard came back empty, but the window's title reads %r --" % caption[:40])
            print("the text DID arrive and the read-back is what failed. Reporting that,")
            print("not a typing failure.")
            return 4

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
