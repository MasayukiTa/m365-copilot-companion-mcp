# -*- coding: utf-8 -*-
"""A synthetic click, type or press acts only on the window the caller named.

THE FINDING THIS PINS. On a new machine the product's own screen tools drove the real
desktop while the window stacking changed underneath them twice: a terminal came to the front
between the look and the click, and a click computed for one window landed once in an
unrelated mail window and once in a chat window. Nothing was typed into the wrong place that
time, but a click into somebody else's mail is not a tolerable near miss -- it has to be
impossible.

THE RULE, MADE FALSIFIABLE. tools/foreground_guard.evaluate decides allow/refuse from plain
facts about the foreground window -- its title, class, owning process and rectangle -- plus
the application the caller said it meant and, for a click, the point. That is exactly the set
of facts a test can fabricate, so every interesting case is here without a desktop: a window
that matches, one that does not, a terminal in front, a click outside the window, no
expectation at all, and the one operator opt-out. The live desktop is read in
tools/window_probe.foreground_facts and fed to this same function; keeping the judgement pure
is what lets these run anywhere.
"""
import pytest

from tools import foreground_guard as FG


def _fg(title="", cls="", process_name="", rect=(0, 0, 800, 600), present=True):
    left, top, right, bottom = rect
    return FG.Foreground(present, title, cls, process_name, left, top, right, bottom)


# ------------------------------------------------------------------ the match itself
def test_a_matching_process_name_is_allowed():
    fg = _fg(title="Untitled - Notepad", cls="Notepad", process_name="notepad.exe")
    assert FG.evaluate("notepad.exe", fg, point=(100, 100)) is None
    # with or without the extension on either side
    assert FG.evaluate("notepad", fg, point=(100, 100)) is None


def test_a_matching_title_pattern_is_allowed():
    """A title substring and a regular expression both count -- a window an app retitles
    freely is still matchable by what the caller can see."""
    fg = _fg(title="Document1 - Word", cls="OpusApp", process_name="winword.exe")
    assert FG.evaluate("Word", fg, point=(10, 10)) is None
    assert FG.evaluate(r"Document\d+ - Word", fg, point=(10, 10)) is None


def test_the_wrong_window_in_front_is_refused():
    """THE EXACT FINDING: the click was computed for one app, a mail window is in front."""
    mail = _fg(title="Inbox - Outlook", cls="rctrl_renwnd32", process_name="outlook.exe")
    refusal = FG.evaluate("notepad.exe", mail, point=(100, 100))
    assert refusal and "does not match" in refusal
    assert "outlook.exe" in refusal       # it names what was actually in front


def test_a_chat_window_in_front_is_refused_too():
    chat = _fg(title="Chat | Teams", cls="TeamsWebView", process_name="ms-teams.exe")
    assert FG.evaluate("notepad", chat, point=(100, 100)) is not None


# ------------------------------------------------------------------ the point must be inside
def test_a_point_outside_the_matched_window_is_refused():
    """Even the right application refuses a click that falls outside its rectangle: the pixel
    is over something else, whatever owns the foreground."""
    fg = _fg(title="Untitled - Notepad", process_name="notepad.exe", rect=(0, 0, 400, 300))
    assert FG.evaluate("notepad.exe", fg, point=(10, 10)) is None
    refusal = FG.evaluate("notepad.exe", fg, point=(500, 10))
    assert refusal and "outside the foreground window" in refusal


def test_type_and_press_have_no_point_and_only_check_identity():
    """type/press pass point=None: there is no coordinate, only the focused window matters."""
    fg = _fg(title="Untitled - Notepad", process_name="notepad.exe", rect=(0, 0, 400, 300))
    assert FG.evaluate("notepad.exe", fg, point=None) is None
    assert FG.evaluate("outlook", fg, point=None) is not None


# ------------------------------------------------------------------ a terminal is special
def test_a_terminal_in_front_is_refused_unless_named():
    """The window class that actually drifted in front. A bare expectation that is not a
    terminal does not make a console a legitimate target."""
    term = _fg(title="C:\\", cls="CASCADIA_HOSTING_WINDOW_CLASS",
               process_name="WindowsTerminal.exe")
    # Asking for notepad, with a terminal in front, refuses and says why.
    refusal = FG.evaluate("notepad.exe", term, point=(10, 10))
    assert refusal and "terminal/console" in refusal


def test_a_terminal_is_allowed_only_when_asked_for_by_name():
    term = _fg(title="Windows PowerShell", cls="ConsoleWindowClass",
               process_name="powershell.exe")
    assert FG.evaluate("powershell.exe", term, point=(10, 10)) is None
    assert FG.evaluate("terminal", term, point=(10, 10)) is None
    # but not by a pattern that only matches it from the opt-out path (see below)


# ------------------------------------------------------------------ no expectation at all
def test_no_expectation_is_refused_by_default():
    fg = _fg(title="Untitled - Notepad", process_name="notepad.exe")
    refusal = FG.evaluate("", fg, point=(10, 10), environ={})
    assert refusal and "no expected application" in refusal
    assert FG.ALLOW_ANY_ENV in refusal          # it tells the operator the one way out


def test_the_operator_opt_out_allows_a_named_nothing_but_still_bars_a_terminal():
    on = {FG.ALLOW_ANY_ENV: "1"}
    ordinary = _fg(title="Untitled - Notepad", process_name="notepad.exe")
    assert FG.evaluate("", ordinary, point=(10, 10), environ=on) is None
    # ...but a console in front is STILL refused, opt-out or not: it is the class most likely
    # to be the thing that drifted in front.
    term = _fg(title="cmd", cls="ConsoleWindowClass", process_name="cmd.exe")
    assert FG.evaluate("", term, point=(10, 10), environ=on) is not None


def test_the_opt_out_is_off_for_anything_but_an_affirmative_word():
    ordinary = _fg(process_name="notepad.exe")
    for raw in ("", "0", "false", "off", "no", "maybe", "2"):
        assert FG.evaluate("", ordinary, point=(1, 1),
                           environ={FG.ALLOW_ANY_ENV: raw}) is not None, raw
    for raw in ("1", "true", "on", "yes", "YES", "On"):
        assert FG.evaluate("", ordinary, point=(1, 1),
                           environ={FG.ALLOW_ANY_ENV: raw}) is None, raw


# ------------------------------------------------------------------ nothing in front
def test_nothing_in_the_foreground_is_refused():
    """The lock screen and a UAC prompt leave no foreground window; there is nothing to act on,
    with or without an expectation."""
    none = _fg(present=False)
    assert FG.evaluate("notepad.exe", none, point=(1, 1)) is not None
    assert FG.evaluate("", none, point=(1, 1), environ={FG.ALLOW_ANY_ENV: "1"}) is not None


def test_an_empty_expectation_never_matches_everything():
    """The failure mode a catch-all would hide: '' must not be read as 'any window'."""
    assert not FG.matches_expectation("", "Inbox - Outlook", "rctrl_renwnd32", "outlook.exe")


def test_a_broken_regex_that_is_not_a_substring_does_not_match():
    """An unanchored '(' is not a substring of the title and not a valid pattern -- it must
    fail closed rather than raise."""
    fg = _fg(title="Untitled - Notepad", process_name="notepad.exe")
    assert FG.matches_expectation("(unclosed", fg.title, fg.cls, fg.process_name) is False
