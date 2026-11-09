# -*- coding: utf-8 -*-
"""The fleet's real-desktop guard: ALLOW by default, with an explicit deny opt-out.

The owner cancelled the ported-UIA A/B test ("uiaab"/"uiaab2") when it opened Notepad and
Calculator on their screen. relay/desktop_guard.py classifies that KIND of goal. Owner
instruction 2026-11-09: FLIP THE DEFAULT so a matching goal is ALLOWED, and keep an opt-out
(desktop_guard=deny in settings.txt, or env MCP_DESKTOP_GUARD=deny) that restores the old
refuse-by-default behaviour -- under which a matching goal is refused unless it carries the
explicit operator field allow_real_desktop.

These tests drive the guard with the ACTUAL goal files the owner's two jobs used
(the per-user ~/.claude/tools/goals/g_uia_ab*.txt files) and with ordinary coding/data goals.
The classifier is a deterministic vocabulary match (judge_gate shape): no clock, no network,
no real desktop touched here.
"""

import io
import os

import pytest

from relay import desktop_guard as dg


#: The real goal files. When present (the operator's machine) they are the authoritative
#: wording; when absent (a fresh CI checkout -- the goals/ tree is not in the repo) the test
#: falls back to verbatim excerpts captured from them so the assertion still runs everywhere.
_GOAL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "tools", "goals")

_FALLBACK = {
    # Verbatim phrases from g_uia_ab.txt (the real-desktop run).
    "g_uia_ab.txt": (
        "Your one task: an A/B test of two versions of our UIA tool file desktop_act.py "
        "before anyone applies the new one. on Windows Notepad and Calculator only "
        "(open them yourself, close them yourself)."
    ),
    # Verbatim phrases from g_uia_ab2.txt (the 'private desktop' re-run; still matches because
    # it is the same computer-use test and names the owner's real desktop/taskbar).
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
def _clean_env(monkeypatch):
    # Start each test from the shipped DEFAULT (allow): clear both the new and legacy env vars
    # and drop the mtime cache so a prior test's reading cannot leak in. Individual tests that
    # exercise the opt-out set MCP_DESKTOP_GUARD themselves.
    monkeypatch.delenv("MCP_DESKTOP_GUARD", raising=False)
    monkeypatch.delenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", raising=False)
    dg._reset_for_test()
    yield
    dg._reset_for_test()


@pytest.mark.parametrize("name", ["g_uia_ab.txt", "g_uia_ab2.txt"])
def test_the_owners_two_uia_goals_still_classify_as_real_desktop(name):
    # The classifier is unchanged: both still MATCH. What changed is the default disposition.
    text = _goal_text(name)
    assert dg.asks_for_real_desktop(text) is True


@pytest.mark.parametrize("name", ["g_uia_ab.txt", "g_uia_ab2.txt"])
def test_matching_goals_are_allowed_by_default(name):
    # Owner instruction 2026-11-09: the flipped default ALLOWS a matching goal (no refusal).
    text = _goal_text(name)
    assert dg.mode() == "allow"
    assert dg.refuse_reason({"text": text}) is None


def test_ordinary_goals_are_not_classified_and_not_refused():
    for g in [
        "Fix the CSV parser in tools/data_loader.py and add a unit test.",
        "Summarize the quarterly sales spreadsheet and build a bar chart.",
        "Refactor relay/fanout.py to carry parent_jid and write a test.",
        "Open the budget workbook, recompute the totals, and email the report.",
    ]:
        assert dg.asks_for_real_desktop(g) is False, g
        assert dg.refuse_reason({"text": g}) is None, g


def test_deny_opt_out_restores_the_old_refusal(monkeypatch):
    # The opt-out via env: desktop_guard=deny -> a matching goal is refused again.
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "deny")
    dg._reset_for_test()
    assert dg.mode() == "deny"
    text = _goal_text("g_uia_ab.txt")
    reason = dg.refuse_reason({"text": text})
    assert reason is not None
    assert "allow_real_desktop" in reason  # the refusal names the one per-goal override
    assert "desktop_guard" in reason       # ...and the opt-out to remove


def test_under_deny_the_operator_override_lets_one_goal_through(monkeypatch):
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "deny")
    dg._reset_for_test()
    text = _goal_text("g_uia_ab.txt")
    # Same forbidden goal, but the operator attached allow_real_desktop -> allowed.
    assert dg.refuse_reason({"text": text, "allow_real_desktop": True}) is None
    # A false/absent value does not count as set -> still refused under deny.
    assert dg.refuse_reason({"text": text, "allow_real_desktop": False}) is not None
    assert dg.refuse_reason({"text": text}) is not None


def test_deny_does_not_refuse_an_ordinary_goal(monkeypatch):
    # Even under the opt-out, a goal that does not ask for real-desktop automation is allowed.
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "deny")
    dg._reset_for_test()
    assert dg.refuse_reason({"text": "Fix the CSV parser and add a unit test."}) is None


def test_a_missing_or_unknown_switch_reads_as_the_default_allow(monkeypatch):
    # Owner's flipped default: an absent/garbage value is ALLOW, not deny. This is also the
    # fail-open direction, so a missing or garbled setting never starts refusing work.
    monkeypatch.delenv("MCP_DESKTOP_GUARD", raising=False)
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "maybe")
    dg._reset_for_test()
    assert dg.mode() == "allow"


def test_legacy_switch_on_still_means_deny(monkeypatch):
    # Back-compat: an explicit on of the previous env var still restores the refusal.
    monkeypatch.setenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", "on")
    dg._reset_for_test()
    assert dg.mode() == "deny"
    text = _goal_text("g_uia_ab.txt")
    assert dg.refuse_reason({"text": text}) is not None


def test_new_env_beats_legacy_env(monkeypatch):
    # If both are set, the new MCP_DESKTOP_GUARD is decisive.
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "allow")
    monkeypatch.setenv("MCP_BLOCK_REAL_DESKTOP_AUTOMATION", "on")
    dg._reset_for_test()
    assert dg.mode() == "allow"
    text = _goal_text("g_uia_ab.txt")
    assert dg.refuse_reason({"text": text}) is None


def test_a_goal_object_not_only_a_dict_is_classified_under_deny(monkeypatch):
    monkeypatch.setenv("MCP_DESKTOP_GUARD", "deny")
    dg._reset_for_test()

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
