# -*- coding: utf-8 -*-
"""The ordinary things: press Windows, press Win+R, type a command, open an application.

WHAT THE EARLIER PROOF DID NOT COVER, AND WHY THAT MATTERED. scripts/prove_click_and_type.py
opens a window, clicks inside it and types into it. Every one of those lands in an ordinary
application that this process started. None of them touches the SHELL -- the Start menu, the
Run dialog, a console -- and the shell is exactly where synthetic input is most likely to be
refused: it runs at a different integrity level, it takes focus away from whatever asked for
it, and the Windows key is the one modifier that leaves the focused application entirely.
"Clicking and typing work" was therefore a claim about a narrower thing than it sounded.

WHAT IS CHECKED, AND HOW. Not "the keystroke was sent" -- SendInput returning success says
only that the OS accepted it, which it does even when nothing acts on it. Every step is
checked by looking at the desktop afterwards: a window that was not there before, with the
class or title it should have. A step that cannot be confirmed that way is reported as
unconfirmed rather than as a pass.

IT CLEANS UP AFTER ITSELF AND ONLY AFTER ITSELF. Anything it opens, it closes, by the window
handle it watched appear -- never by name, because killing "every console" would take the
operator's own. A step that leaves something on screen says so.
"""
from __future__ import annotations

import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import desktop_input as DI
from tools import window_probe as W
from tools.screen_capture import capture_reports_what_it_did

#: The Run dialog and most Win32 dialogs are class #32770. Checking the class rather than the
#: title keeps this working on a non-Japanese install and on a future rename.
DIALOG_CLASS = "#32770"

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E


def _control_text(hwnd) -> str:
    """Read a control's text from ANOTHER process, which GetWindowTextW cannot do.

    GetWindowTextW returns the caption for a top-level window, but for a child control owned
    by a different process it returns nothing at all -- so "the Run box is empty" and "we are
    not allowed to read the Run box" look identical through it. WM_GETTEXT asks the control
    itself and works across processes.

    This is what turns "typing did not work" from a conclusion into a measurement: after
    typing, either the characters are in the box or they are not, and the answer is readable.
    """
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    n = u.SendMessageW(wintypes.HWND(hwnd), WM_GETTEXTLENGTH, 0, 0)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.SendMessageW(wintypes.HWND(hwnd), WM_GETTEXT, n + 1, ctypes.byref(buf))
    return buf.value


def _dialog_text(dlg) -> str:
    """Whatever is typed into a dialog, from whichever child holds it.

    The Run dialog's box is a ComboBox with an Edit inside it, and which of the two carries
    the text differs between Windows versions. Asking every child and taking the first
    non-empty answer is stable across that, and cheap: a dialog has a handful of children.
    """
    for child in W.children_of(dlg.hwnd, min_side=1):
        t = _control_text(child.hwnd).strip()
        if t:
            return t
    return ""


def _windows():
    return {t.hwnd: t for t in W.top_level_windows(min_side=80)}


def _wait_for_new(before, deadline_s=8.0, predicate=None):
    """Wait for a top-level window that was not there before. Returns it, or None."""
    end = time.time() + deadline_s
    while time.time() < end:
        for hwnd, t in _windows().items():
            if hwnd not in before and (predicate is None or predicate(t)):
                return t
        time.sleep(0.25)
    return None


def _pid_of(hwnd):
    import ctypes
    from ctypes import wintypes

    owner = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
    return owner.value


def _close(hwnd, label):
    pid = _pid_of(hwnd)
    if not pid:
        print("   could not identify the process behind %s; leaving it" % label)
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
        print("   closed %s (pid %d)" % (label, pid))
    except Exception as e:
        print("   could not close %s (pid %d): %s" % (label, pid, e))


results = []


def record(step, ok, detail):
    results.append((step, ok, detail))
    print("   %s  %s" % ("PASS" if ok else "FAIL", detail))


def step_windows_key():
    """Press Windows on its own: the Start menu should come up, and Escape should dismiss it.

    The check is the FOREGROUND WINDOW CHANGING, not a new top-level window: the Start menu
    on Windows 11 is hosted by an already-running process (SearchHost / StartMenuExperience),
    so "a window appeared" is the wrong question and would report a false failure.
    """
    print("\n[1] Windows key -> Start")
    before = W.foreground_window()
    before_label = before.label() if before else "(nothing)"
    try:
        DI.press("win")
    except DI.InputRefused as e:
        record("win", False, "refused: %s" % e)
        return
    time.sleep(1.2)
    after = W.foreground_window()
    after_label = after.label() if after else "(nothing)"
    changed = (after is None) != (before is None) or (
        after is not None and before is not None and after.hwnd != before.hwnd)
    record("win", changed,
           "foreground %s -> %s" % (before_label[:40], after_label[:40]))
    DI.press("escape")
    time.sleep(0.8)
    back = W.foreground_window()
    print("   after Escape, foreground is %s" % (back.label()[:40] if back else "(nothing)"))


def step_win_r_and_open(command, expect_class=None, label=""):
    """Win+R, type a command, Enter, and wait for the application to appear."""
    print("\n[2] Win+R -> type %r -> Enter" % command)
    before = _windows()
    try:
        DI.press("win", "r")
    except DI.InputRefused as e:
        record("win+r", False, "refused: %s" % e)
        return None
    dlg = _wait_for_new(before, 6.0, lambda t: t.cls == DIALOG_CLASS)
    if dlg is None:
        # Not necessarily a failure of Win+R: the dialog is small and may fall under the
        # min_side filter, or it may already have been open. Say which is unknown.
        record("win+r", False,
               "no %s dialog appeared within 6s -- Win+R may not have reached the shell"
               % DIALOG_CLASS)
        return None
    record("win+r", True, "Run dialog appeared: %s" % dlg.label()[:50])

    # LET IT TAKE FOCUS BEFORE TYPING. _wait_for_new returns the instant the window exists,
    # which is earlier than the instant it is ready to receive keystrokes; typing into that
    # gap goes nowhere and looks exactly like typing being broken.
    time.sleep(0.8)
    fg = W.foreground_window()
    if fg is None or fg.hwnd != dlg.hwnd:
        record("focus before typing", False,
               "the Run dialog is not in front (%s has focus) -- someone else is using this "
               "desktop, or something stole focus. NOT a result about typing."
               % (fg.label()[:40] if fg else "nothing"))
        DI.press("escape")
        return None

    n = DI.type_text(command)
    time.sleep(0.5)
    # READ IT BACK. Either the characters are in the box or they are not; without this,
    # "no application appeared" cannot be told apart from "the text never arrived", and
    # those have entirely different causes.
    typed = _dialog_text(dlg)
    if typed != command:
        record("text reached the box", False,
               "sent %r, the box holds %r" % (command, typed))
        DI.press("escape")
        return None
    record("text reached the box", True, "the box holds %r" % typed)

    before2 = _windows()
    DI.press("enter")
    app = _wait_for_new(before2, 10.0,
                        lambda t: (expect_class is None or expect_class.lower() in t.cls.lower()))
    if app is None:
        record("open %s" % (label or command), False,
               "typed %d chars and pressed Enter, but no matching window appeared in 10s" % n)
        return None
    record("open %s" % (label or command), True,
           "%s at (%d, %d) %dx%d" % (app.label()[:44], app.left, app.top, app.width, app.height))
    return app


def main():
    print(capture_reports_what_it_did())
    print("This moves the operator's focus and opens applications. Everything it opens, it closes.")

    opened = []
    try:
        step_windows_key()

        # A console, which is the case the operator named and also the most demanding: it is
        # a different integrity level from an ordinary app and a different input path.
        app = step_win_r_and_open("cmd", expect_class="ConsoleWindowClass", label="cmd")
        if app:
            opened.append((app.hwnd, "cmd"))

        # And an ordinary application, so that a failure above can be attributed to the
        # console rather than to Win+R.
        app2 = step_win_r_and_open("notepad", expect_class="Notepad", label="notepad")
        if app2:
            opened.append((app2.hwnd, "notepad"))
    except DI.InputRefused as e:
        print("\nREFUSED: %s" % e)
    finally:
        DI.release_all_modifiers()
        print()
        for hwnd, label in opened:
            _close(hwnd, label)

    print()
    print("=== summary ===")
    for step, ok, detail in results:
        print("  %-22s %s  %s" % (step, "PASS" if ok else "FAIL", detail[:90]))
    bad = [s for s, ok, _ in results if not ok]
    print()
    if not results:
        print("nothing was measured.")
        return 2
    if bad:
        print("NOT SOUND: %s did not do what it should. Computer use cannot open an "
              "application on this machine until that is understood." % ", ".join(bad))
        return 1
    print("SOUND: the shell-level actions work -- Windows key, Win+R, typing a command, and "
          "an application appearing because of it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
