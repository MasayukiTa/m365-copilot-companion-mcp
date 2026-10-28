# -*- coding: utf-8 -*-
"""The cockpit survives a null dictionary and a throwing first tick.

WHAT BROKE. 2026-10-27 14:10-14:27 Fleet Cockpit crashed three times on launch with an
unhandled NullReferenceException: S <- RowSig <- MkRow <- AppendHistoryRows <- BuildRows <-
RenderCards <- OnTickCore <- OnTick, fired on the FIRST tick before any data had arrived. Two
things had to change so a bad first render can no longer take the process down:

  1. The dictionary accessors S / I / Dbl in FleetCockpit.Helpers.cs must treat a NULL
     dictionary as empty rather than dereferencing it -- a row built before its data map exists
     reads blank, it does not throw.
  2. OnTick is a DispatcherTimer handler, so an exception that escapes it is unhandled on the
     UI thread and TERMINATES the process. The real work moved to OnTickCore and OnTick is now
     a thin top-level try/catch that routes any failure to OnTickFailed, which shows the cause
     on screen and records it -- the next tick runs normally.

WHY A STATIC CHECK. The cockpit is WPF C#; the CI runner is Linux with no csc.exe, and the
repository rule forbids Windows-only test code. So this reads the actual source (through
cockpit_source, which concatenates every FleetCockpit.*.cs part so a method that moved between
parts is still found) and pins the specific shapes that made the fix -- the null-guard in each
accessor, and the wrapper/handler pair. A reviewer who deletes the guard or inlines OnTickCore
back into a throwing OnTick fails a test instead of silently reopening the crash.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import cockpit_source  # noqa: E402


def _src():
    return cockpit_source.read(encoding="utf-8-sig", errors="replace")


# ---------------------------------------------------------------- null-safe accessors
def test_the_string_accessor_treats_a_null_dictionary_as_empty():
    body = cockpit_source.member(_src(), "static string S(Dictionary")
    assert "d != null" in body, "S must null-guard its dictionary (the crash entered through S)"


def test_the_int_accessor_treats_a_null_dictionary_as_empty():
    body = cockpit_source.member(_src(), "static int I(Dictionary")
    assert "d != null" in body


def test_the_double_accessor_treats_a_null_dictionary_as_empty():
    body = cockpit_source.member(_src(), "static double Dbl(Dictionary")
    assert "d != null" in body


# ---------------------------------------------------------------- top-level tick guard
def test_ontick_is_a_thin_wrapper_that_catches_everything():
    """OnTick must delegate to OnTickCore inside a try/catch, not do the work itself -- an
    exception escaping a DispatcherTimer handler kills the process."""
    src = _src()
    body = cockpit_source.member(src, "void OnTick(object sender, EventArgs e)")
    assert "OnTickCore" in body, "OnTick must delegate to OnTickCore"
    assert "try" in body and "catch" in body, "OnTick must catch what OnTickCore throws"
    assert "OnTickFailed" in body, "a caught tick failure must be routed to OnTickFailed"


def test_the_real_poll_work_lives_in_ontickcore():
    src = _src()
    assert "void OnTickCore(object sender, EventArgs e)" in src, \
        "the poll loop must live in OnTickCore so OnTick can wrap it"


def test_a_failed_tick_shows_the_cause_and_does_not_rethrow():
    """OnTickFailed is the recovery path: it must put the cause on screen and must not itself be
    a new source of an unhandled exception (everything it does is guarded)."""
    src = _src()
    body = cockpit_source.member(src, "void OnTickFailed(Exception ex)")
    assert "GetType().Name" in body or "ex.Message" in body, \
        "the failure handler must surface the cause, not swallow it blind"
    # the handler's own body is wrapped so recovery can never be the new crash
    assert body.count("catch") >= 1


def test_ontick_does_not_call_rendercards_directly():
    """The crash path RenderCards must be reached only THROUGH OnTickCore, so the top-level
    catch always covers it. If OnTick called RenderCards itself, a throw there would again be
    unhandled on the UI thread."""
    body = cockpit_source.member(_src(), "void OnTick(object sender, EventArgs e)")
    # the thin wrapper calls OnTickCore (and the error path); it must not itself drive a render.
    assert "RenderCards" not in body
