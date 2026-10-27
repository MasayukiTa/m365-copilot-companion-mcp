# -*- coding: utf-8 -*-
"""Pressing Open on a card STARTS the main chat when it is not running.

THE BUG. A card's Open (and the history / conversation open paths) only wrote a request into
.fleet/open.json. The one process that READS open.json is the main chat window (CopilotChat).
With the main window not running, the signal landed nowhere: the button did nothing at all.

THE FIX. The cockpit now ensures the main chat is running (start it if absent, otherwise bring
it to the front) right before it writes open.json, through one shared helper, EnsureMainChatRunning.
Because the helper starts ONLY CopilotChat.exe -- the surface a card Open targets -- it does not
reopen the cockpit or anything else, so the old "every button opened everything" behaviour does
not come back.

THE OTHER HALF lives in the chat window: a chat launched for a card Open must act on the
open.json that was just written. On start it treats a FRESH open.json (<= 10s old) as unseen so
the first poll opens the clicked surface, while a STALE one (a previous session's leftover) is
seeded as already-seen so a plain launch does not replay an old conversation.

These are SOURCE-STRING checks, matching the other ui/ tests.
"""
from __future__ import annotations

import io
import os

from tools import cockpit_source
from tools import ui_source

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.join(REPO, "ui", "FleetCockpit.cs")
CHAT = os.path.join(REPO, "ui", "CopilotChat.cs")


def _read(path):
    if path == COCKPIT:
        return cockpit_source.read(encoding="utf-8")
    if path == CHAT:
        return ui_source.chat_window_source(encoding="utf-8")
    return io.open(path, encoding="utf-8").read()


def _code(path):
    return "\n".join(ln for ln in _read(path).splitlines()
                     if not ln.strip().startswith("//"))


def _method(path, signature, stop="\n    // "):
    src = _read(path)
    i = src.index(signature)
    j = src.find(stop, i + 1)
    return src[i:j] if j > 0 else src[i:]


def test_the_cockpit_has_one_helper_that_starts_the_main_chat():
    body = _code(COCKPIT)
    assert "void EnsureMainChatRunning()" in body
    helper = _method(COCKPIT, "void EnsureMainChatRunning()")
    assert 'GetProcessesByName("CopilotChat")' in helper
    assert 'CopilotChat.exe' in helper
    assert "Process.Start(exe)" in helper
    assert "SetForegroundWindow" in helper


def test_each_card_open_path_ensures_the_main_chat_first():
    for sig in (
        "void OpenConv(string url)",
        "void OpenWorker(string name, string url, int turnIndex)",
        "void OpenHistory(string name, string url, string transcript)",
    ):
        body = _method(COCKPIT, sig)
        assert "EnsureMainChatRunning();" in body, sig
        assert body.index("EnsureMainChatRunning();") < body.index(
            "File.WriteAllText(_openPath"), sig


def test_open_main_reuses_the_same_helper():
    body = _method(COCKPIT, "void OpenMain()")
    assert "EnsureMainChatRunning();" in body


def test_a_fresh_open_request_is_acted_on_at_launch():
    body = _code(CHAT)
    assert "_openMtime = fresh ? 0 : t;" in body
    assert "TimeSpan.FromSeconds(10).Ticks" in body


def test_a_stale_open_request_is_not_replayed_at_launch():
    src = _read(CHAT)
    i = src.index("bool fresh =")
    window = src[i:i + 400]
    assert "fresh ? 0 : t" in window
