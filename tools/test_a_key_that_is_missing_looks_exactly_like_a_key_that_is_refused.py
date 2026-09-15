# -*- coding: utf-8 -*-
"""The keyboard table must be complete, because an absent key is an invisible refusal.

WHAT HAPPENED, 2026-09-15. A worker driving the desktop needed Win+M to get a window out of
the way and could not: `m` was not in VK. Nothing said so in a way anyone would find --
the call came back "unknown key 'm'", which reads like a rule, and the agent went looking
for another route instead of reporting a gap.

VK had thirteen letters in it. Not thirteen chosen letters: the thirteen I had happened to
need while writing the module, a c d e f l n r s v w x z. Win+E, Win+M, Win+I, Ctrl+B,
Ctrl+G, Alt+Tab combinations involving any other letter -- none of them could be pressed,
and none of them was a decision.

THE PROPERTY THESE TESTS HOLD is that the table is COMPLETE and the refusals are EXPLICIT.
Those two together are what makes the module's behaviour legible: if a key does not work,
it is because _REFUSED_COMBOS names it and says why, never because nobody typed it in.

Nothing here sends input. Every assertion is about the table and the refusal decision,
which are reached before any SendInput call.
"""
from __future__ import annotations

import os
import string
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import desktop_input as di  # noqa: E402


def test_every_letter_is_present():
    """The actual regression: thirteen of twenty-six were missing."""
    missing = [c for c in string.ascii_lowercase if c not in di.VK]
    assert missing == [], "letters that cannot be pressed: %s" % missing


def test_every_digit_is_present():
    missing = [c for c in string.digits if c not in di.VK]
    assert missing == [], missing


def test_the_whole_function_row_is_present():
    """F13-F24 exist on real keyboards and are what remapping software targets."""
    missing = ["f%d" % n for n in range(1, 25) if "f%d" % n not in di.VK]
    assert missing == [], missing


def test_letters_and_digits_map_to_the_codes_windows_uses():
    """Generated, so the risk is a wrong formula rather than a wrong entry."""
    assert di.VK["a"] == 0x41 and di.VK["z"] == 0x5A
    assert di.VK["0"] == 0x30 and di.VK["9"] == 0x39
    assert di.VK["f1"] == 0x70 and di.VK["f24"] == 0x87
    assert di.VK["numpad0"] == 0x60 and di.VK["numpad9"] == 0x69


def test_no_two_names_disagree_about_the_same_key():
    """Aliases are fine; an alias pointing at the wrong code is a silent wrong keypress."""
    for a, b in (("ctrl", "control"), ("esc", "escape"), ("del", "delete"),
                 ("win", "lwin"), ("alt", "menu"), ("comma", ","), ("period", "."),
                 ("slash", "/"), ("backslash", "\\"), ("pgup", "pageup")):
        assert di.VK[a] == di.VK[b], (a, b, di.VK[a], di.VK[b])


def test_the_keys_that_would_arrive_as_numpad_twins_carry_the_extended_flag():
    """An arrow delivered without it types a digit -- visible only as wrong text."""
    for name in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown",
                 "delete", "insert", "rwin", "apps", "rctrl", "ralt"):
        assert di.VK[name] in di._EXTENDED, name


def test_shortcuts_that_end_someones_work_are_still_refused():
    """Completeness must not have widened what the module is willing to do."""
    for combo in (("win", "l"), ("alt", "f4"), ("ctrl", "alt", "delete"),
                  ("ctrl", "shift", "escape")):
        with pytest.raises(di.InputRefused):
            di.press(*combo)


def test_a_refusal_is_about_the_combination_not_the_order_it_was_typed():
    with pytest.raises(di.InputRefused):
        di.press("l", "win")
    with pytest.raises(di.InputRefused):
        di.press("delete", "ctrl", "alt")


def test_an_unknown_key_suggests_rather_than_reciting_the_whole_table():
    """167 names is several hundred characters into a conversation for one typo."""
    with pytest.raises(di.InputRefused) as e:
        di.press("ctrk")
    msg = str(e.value)
    assert "ctrl" in msg, msg
    assert len(msg) < 400, "the error grew back into a listing: %d chars" % len(msg)


def test_the_cleanup_lets_go_of_every_modifier_that_can_be_pressed():
    """A modifier that can be held and is not released is residue on someone else's machine.

    Read out of the function rather than executed, because executing it sends key-up events
    to whatever the operator has in front right now.
    """
    import inspect

    src = inspect.getsource(di.release_all_modifiers)
    pressable = [n for n, vk in di.VK.items()
                 if vk in (0x11, 0x10, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
                           0x5B, 0x5C)]
    released = {n for n in pressable if '"%s"' % n in src}
    covered = {di.VK[n] for n in released}
    assert covered == {di.VK[n] for n in pressable}, (
        "a modifier can be pressed but not released: %s"
        % sorted(set(pressable) - {n for n in pressable if di.VK[n] in covered}))
