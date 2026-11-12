# -*- coding: utf-8 -*-
"""Before a synthetic click, type or key press, prove the foreground is the window meant.

WHY THIS EXISTS. On a new machine the product's own screen tools drove the real desktop
while the window stacking changed underneath them twice: a terminal came to the front and
shifted the coordinates, and a click that had been computed for one window landed once in an
unrelated mail window and once in a chat window. Nothing was typed or sent, but a click into
somebody else's mail is not a near miss to be tolerated -- it must be impossible.

THE RULE, STATED ONCE. A click/type/press is allowed only when the caller NAMED the
application it means and the window actually in front right now belongs to that application
-- and, for a click, the point lies inside that window's rectangle. Anything else refuses and
does nothing. The naming is mandatory: with no expectation the only safe action is none,
because the tool cannot tell a correct target from a window that drifted in front of it. The
sole way to act without naming an application is an explicit operator opt-out carried in an
environment variable (MCP_SCREEN_ALLOW_ANY_FOREGROUND), default off, surfaced in the settings
surface so turning it on is a deliberate act and not a thing that happens by accident.

A TERMINAL IN FRONT IS NEVER THE ANSWER BY DEFAULT. The finding was a console stealing the
foreground, so a console or terminal window counts as the expected application only when the
caller asked for it BY NAME. A bare expectation that happens to match nothing, or an opt-out,
does not turn a terminal into a legitimate click target -- the one window class most likely
to be the thing that drifted in front must be opted into explicitly, every time.

WHY THE LOGIC IS PURE AND TAKES ITS FACTS AS ARGUMENTS. Deciding allow/refuse must be
testable without a desktop: the interesting cases are a window that matches, a window that
does not, a terminal in front, and no expectation at all, and all four are expressible as
plain facts about a foreground window. So this module computes the decision from values
(title, class, process name, rectangle, click point) that the caller reads from the live
desktop at the moment of acting and a test supplies by hand. tools/desktop_input.py and
tools/screen_ops.py read the live foreground and call in here; the OS reads live there, the
judgement lives here.
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple, Optional

#: The one opt-out. Default OFF: absent, empty or anything that is not an affirmative word
#: leaves the guard requiring an expectation. The settings surface (.env.example) names it so
#: an operator can turn it on knowingly; nothing turns it on implicitly.
ALLOW_ANY_ENV = "MCP_SCREEN_ALLOW_ANY_FOREGROUND"

#: Window classes and process names that ARE a terminal/console. Matched case-insensitively.
#: A console in front was the actual finding, so this list exists to make such a window fail
#: the guard unless it was asked for by name -- not to be exhaustive about every shell.
_CONSOLE_CLASSES = frozenset({
    "consolewindowclass",            # the classic conhost console window
    "pseudoconsolewindow",
    "cascadia_hosting_window_class", # Windows Terminal
})
_CONSOLE_PROCESS_NAMES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "windowsterminal.exe", "wt.exe", "bash.exe", "sh.exe", "wsl.exe",
    "mintty.exe", "alacritty.exe", "wezterm-gui.exe", "cmder.exe",
})


class Foreground(NamedTuple):
    """What the live desktop said is in front, reduced to the facts the guard judges on.

    Every field is something the OS states and a test can fabricate. ``present`` is False when
    there is no foreground window at all (the lock screen, a UAC prompt) -- a state in which no
    expectation can be satisfied and the action must not go ahead.
    """

    present: bool
    title: str
    cls: str
    process_name: str
    left: int
    top: int
    right: int
    bottom: int

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def label(self) -> str:
        name = (self.title or "").strip()
        proc = (self.process_name or "").strip()
        bits = []
        if proc:
            bits.append(proc)
        bits.append("'%s'" % name[:40] if name else "(untitled)")
        if self.cls:
            bits.append("class %s" % self.cls)
        return " ".join(bits)


def allow_any_foreground(environ=None) -> bool:
    """Is the explicit opt-out turned on? Default off.

    Affirmative only on an explicit truthy word, so a stray value never silently disables the
    guard. Absent, empty, ``0``, ``false``, ``off``, ``no`` all read as off.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ALLOW_ANY_ENV) or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def looks_like_terminal(cls: str, process_name: str) -> bool:
    """Is this window a terminal/console? Class or process name is enough."""
    c = (cls or "").strip().lower()
    p = (process_name or "").strip().lower()
    return c in _CONSOLE_CLASSES or p in _CONSOLE_PROCESS_NAMES


def _expectation_names_a_terminal(expect: str) -> bool:
    """Did the caller ASK FOR a terminal by name? Only then may a console be the target.

    Checked against the same console vocabulary, plus the bare words an operator would use
    ("terminal", "console", "cmd", "powershell", ...). A pattern that matches a console only by
    accident -- say an empty or catch-all pattern -- does not count: the caller has to have
    written something that denotes a terminal.
    """
    e = (expect or "").strip().lower()
    if not e:
        return False
    vocab = set(_CONSOLE_PROCESS_NAMES) | set(_CONSOLE_CLASSES) | {
        "terminal", "console", "cmd", "command prompt", "powershell", "pwsh",
        "bash", "wsl", "mintty", "conhost", "windows terminal",
    }
    return any(word in e or e in word for word in vocab)


def matches_expectation(expect: str, title: str, cls: str, process_name: str) -> bool:
    """Does the window (title/class/process) belong to the application the caller named?

    ``expect`` is a process name or a window-title pattern. It matches when:
      * it equals the process name (case-insensitive, with or without a trailing .exe), or
      * it is a case-insensitive substring of the title or class, or
      * it is a valid regular expression that searches the title or class.
    An empty expectation matches nothing -- that is handled by the caller as "no expectation",
    never as "matches everything".
    """
    e = (expect or "").strip()
    if not e:
        return False
    el = e.lower()
    proc = (process_name or "").strip().lower()
    if proc:
        # Process name matches with or without the .exe on either side.
        proc_stem = proc[:-4] if proc.endswith(".exe") else proc
        el_stem = el[:-4] if el.endswith(".exe") else el
        if el == proc or el_stem == proc_stem:
            return True
    title_l = (title or "").lower()
    cls_l = (cls or "").lower()
    if el in title_l or el in cls_l:
        return True
    try:
        rx = re.compile(e, re.IGNORECASE)
    except re.error:
        return False
    return bool(rx.search(title or "") or rx.search(cls or ""))


def evaluate(expect: Optional[str], fg: Foreground,
             point: Optional[tuple] = None, environ=None) -> Optional[str]:
    """Decide allow or refuse. Returns None to allow, or a plain refusal sentence.

    Args:
        expect: the application the caller means -- a process name or a title pattern. Empty or
            None means the caller named nothing.
        fg: the live foreground window, as facts.
        point: (x, y) desktop coordinate a click will land on, or None for type/press where
            there is no point and only the focused window matters.
        environ: for tests; defaults to the real process environment.

    The order is deliberate: the no-expectation rule is decided first, because without a named
    application there is nothing to compare the foreground against, and only the opt-out can
    rescue it. Everything after assumes an expectation exists.
    """
    want = (expect or "").strip()

    if not want:
        if not allow_any_foreground(environ):
            return ("refusing to act on the screen: no expected application was given, so the "
                    "foreground cannot be verified as the intended one. Name the application "
                    "(process name or a window-title pattern). To act without naming one, an "
                    "operator must set %s=1 in the settings; it is off by default."
                    % ALLOW_ANY_ENV)
        # Opted out of the expectation, but a terminal is still never a default target, and
        # there must be a real window in front to receive the input.
        if not fg.present:
            return ("refusing to act on the screen: nothing is in the foreground (a locked "
                    "session, screensaver or UAC prompt). There is no window to act on.")
        if looks_like_terminal(fg.cls, fg.process_name):
            return ("refusing to act on the screen: a terminal/console window is in front (%s) "
                    "and no application was named. A console is never the intended target "
                    "unless it is asked for by name." % fg.label())
        return _check_point(None, fg, point)

    # An expectation was given.
    if not fg.present:
        return ("refusing to act on the screen: expected %r in the foreground, but nothing is "
                "in front (a locked session, screensaver or UAC prompt)." % want)

    if looks_like_terminal(fg.cls, fg.process_name):
        # A terminal is in front. It is the expected window only when the caller asked for a
        # terminal -- either by a specific process/title that matches it, or by a generic word
        # that denotes a terminal ("terminal", "console", "cmd", ...). A generic word is enough
        # BECAUSE the foreground is already known to be a terminal; the bar being defended is
        # "never a terminal by accident", not "only ever by exact name".
        named_specifically = matches_expectation(want, fg.title, fg.cls, fg.process_name)
        if not (named_specifically or _expectation_names_a_terminal(want)):
            return ("refusing to act on the screen: a terminal/console window is in front (%s), "
                    "which is never the expected application unless asked for by name. Expected "
                    "%r." % (fg.label(), want))
        return _check_point(want, fg, point)

    if not matches_expectation(want, fg.title, fg.cls, fg.process_name):
        return ("refusing to act on the screen: the foreground window is %s, which does not "
                "match the expected application %r. The window in front changed since it was "
                "seen; capture again and retry against the right window."
                % (fg.label(), want))

    return _check_point(want, fg, point)


def _check_point(want, fg: Foreground, point) -> Optional[str]:
    """For a click: the point must lie inside the verified window. None if ok or no point."""
    if point is None:
        return None
    x, y = int(point[0]), int(point[1])
    if not fg.contains(x, y):
        return ("refusing to click: the point (%d, %d) is outside the foreground window %s at "
                "(%d, %d)-(%d, %d). The click would land in a different window; capture again "
                "and aim inside the intended one."
                % (x, y, fg.label(), fg.left, fg.top, fg.right, fg.bottom))
    return None
