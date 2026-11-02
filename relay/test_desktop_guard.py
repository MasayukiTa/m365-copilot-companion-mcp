# -*- coding: utf-8 -*-
"""The fleet refuses UI automation on the owner's REAL desktop, with the owner's own wording.

The owner cancelled the ported-UIA A/B test ("uiaab"/"uiaab2") and forbade real-desktop
computer use after it opened Notepad and Calculator on their screen. relay/desktop_guard.py
is the standing guard: ON by default, it refuses any goal whose text asks for real-desktop UI
automation unless the goal carries the explicit operator field allow_real_desktop.

These tests drive the guard with the ACTUAL goal files the owner's two jobs used
(C:/Users/M118A8586/.claude/tools/goals/g_uia_ab*.txt) -- both must be refused -- and with
ordinary coding/data goals -- which must NOT be refused. The classifier is a deterministic
vocabulary match (judge_gate shape): no clock, no network, no real desktop touched here.
"""

import io
import os

import pytest

from relay import desktop_guard as dg


#: The real goal files. When present (the operator's machine) they are the authoritative
#: wording; when absent (a fresh CI checkout -- the goals/ tree is not in the repo) the test
#: falls back to verbatim excerpts captured from them so the assertion still runs everywhere.
_GOAL_DIR = r"C:/Users/M118A8586/.claude/tools/goals"

_FALLBACK = {
    # Verbatim phrases from g_uia_ab.txt (the real-desktop run).
    "g_uia_ab.txt": (
        "Your one task: an A/B test of two versions of our UIA tool file desktop_act.py "
        "before anyone applies the new one. on Windows Notepad and Calculator only "
        "(open them yourself, close them yourself)."
    ),
    # Verbatim phrases from g_uia_ab2.txt (the 'private desktop' re-run; still refused because
    # it is the same forbidden computer-use test and names the owner's real desktop/taskbar).
    "g_uia_ab2.txt": (
        "an A/B test of two versions of our UIA tool file desktop_act.py. "
        "the owner's screen, taskbar, mouse pointer and keyboard focus must never be taken. "
        "A previous run opened Notepad and Calculator on the owner's real desktop."
    ),
}


def _goal_text(name):
    path = os.path.join(_GOAL_DIR, name)
    try:
        return io.open(path, encoding="utf-8-sig").read()
    except Exception:
        return _FALLBACK[name]


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    # Pin the guard ON regardless of the operator's settings.txt, and drop the mtime cache so
    # a prior test's reading cannot leak in. The env var is the documented test override.
    monkeypatch.setenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", "on")
    dg._reset_for_test()
    yield
    dg._reset_for_test()


@pytest.mark.parametrize("name", ["g_uia_ab.txt", "g_uia_ab2.txt"])
def test_the_owners_two_uia_goals_are_refused(name):
    text = _goal_text(name)
    assert dg.asks_for_real_desktop(text) is True
    reason = dg.refuse_reason({"text": text})
    assert reason is not None
    assert "allow_real_desktop" in reason  # the refusal names the one override


def test_ordinary_goals_are_not_refused():
    for g in [
        "Fix the CSV parser in tools/data_loader.py and add a unit test.",
        "Summarize the quarterly sales spreadsheet and build a bar chart.",
        "Refactor relay/fanout.py to carry parent_jid and write a test.",
        "Open the budget workbook, recompute the totals, and email the report.",
    ]:
        assert dg.asks_for_real_desktop(g) is False, g
        assert dg.refuse_reason({"text": g}) is None, g


def test_the_operator_override_lets_one_goal_through():
    text = _goal_text("g_uia_ab.txt")
    # Same forbidden goal, but the operator attached allow_real_desktop -> allowed.
    assert dg.refuse_reason({"text": text, "allow_real_desktop": True}) is None
    # A non-bool truthy value also counts as set; absent/false does not.
    assert dg.refuse_reason({"text": text, "allow_real_desktop": False}) is not None
    assert dg.refuse_reason({"text": text}) is not None


def test_off_disables_the_guard_entirely(monkeypatch):
    monkeypatch.setenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", "off")
    dg._reset_for_test()
    text = _goal_text("g_uia_ab.txt")
    assert dg.mode() == "off"
    assert dg.refuse_reason({"text": text}) is None


def test_a_missing_or_unknown_switch_reads_as_on_the_floor(monkeypatch):
    # The owner's rule is a safety floor: an absent/garbage value must NOT silently disable it.
    monkeypatch.delenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", raising=False)
    monkeypatch.setenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", "maybe")
    dg._reset_for_test()
    assert dg.mode() == "on"


def test_a_goal_object_not_only_a_dict_is_classified():
    class _G:
        goal = "automate Notepad and Calculator with desktop_act on the real desktop"
        allow_real_desktop = False
    assert dg.refuse_reason(_G()) is not None
    _G.allow_real_desktop = True
    assert dg.refuse_reason(_G()) is None


def test_non_text_input_is_not_classified():
    assert dg.asks_for_real_desktop(None) is False
    assert dg.asks_for_real_desktop("") is False
    assert dg.asks_for_real_desktop(123) is False
