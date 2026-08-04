"""Pin the invariant that the companion Edge's re-minimize snippets never touch a
non-visible window.

WHY: the companion Edge is launched with --headless=new, which means there is NO
window at all (correct state: no taskbar button). ShowWindow(SW_MINIMIZE=6) called on
a window whose WS_VISIBLE bit is clear does not leave it hidden -- Windows SETS
WS_VISIBLE and shows it minimized, which creates a taskbar button. Two PowerShell
snippets re-minimize the companion Edge window on a timer/on-demand
(scripts/win/edge_keeper.ps1's loop and relay/edge_recover.py's _REHIDE_PS, invoked by
rehide()); both used to guard the ShowWindow(..., 6) call with IsIconic(h) alone, which
is False for a hidden window, so they fired on hidden windows and revealed them. The
fix adds an IsWindowVisible(h) check to both guards.

These are pure text-pinning tests -- no live Edge, no Playwright, no PowerShell
execution -- so a future edit that silently drops the IsWindowVisible guard (or
introduces SW_HIDE, which edge_keeper.ps1's own comment explains would make Edge
discard the tab's renderer and kill the driver mid-drive with TargetClosedError) fails
CI instead of only showing up as a live taskbar-button regression. Run:
    .venv\\Scripts\\python.exe -m pytest relay\\test_edge_rehide_guard.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

from relay.edge_recover import _REHIDE_PS

REPO = Path(__file__).resolve().parent.parent
EDGE_KEEPER_PS1 = REPO / "scripts" / "win" / "edge_keeper.ps1"


def _minimize_guard_line(text: str) -> str:
    """Return the `if (...)` line that guards the ShowWindow(h, 6) (SW_MINIMIZE) call in
    `text`, so assertions check the actual condition rather than merely "this substring
    appears somewhere in the file" (which would also pass if IsWindowVisible were
    declared but never used in the guard)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "ShowWindow(" in line and re.search(r",\s*6\s*\)", line):
            for j in range(i, -1, -1):
                if "if (" in lines[j]:
                    return lines[j]
            raise AssertionError("ShowWindow(..., 6) call has no preceding 'if (' guard")
    raise AssertionError("no ShowWindow(..., 6) [SW_MINIMIZE] call found")


def test_rehide_ps_declares_is_window_visible_pinvoke():
    assert 'extern bool IsWindowVisible(IntPtr h);' in _REHIDE_PS


def test_rehide_ps_minimize_guard_checks_window_visible():
    guard = _minimize_guard_line(_REHIDE_PS)
    assert "IsWindowVisible" in guard, guard
    # IsIconic must stay too: still skip a window that is already minimized.
    assert "IsIconic" in guard, guard


def test_rehide_ps_never_calls_sw_hide():
    # SW_HIDE (0) must never appear: edge_keeper.ps1's own comment records why -- a fully
    # hidden window makes Edge discard the tab's renderer and the driver dies mid-drive
    # with TargetClosedError. SW_MINIMIZE (6) is the only state change allowed.
    assert not re.search(r"ShowWindow\([^)]*,\s*0\s*\)", _REHIDE_PS)


def test_edge_keeper_ps1_declares_is_window_visible_pinvoke():
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    assert 'extern bool IsWindowVisible(IntPtr h);' in text


def test_edge_keeper_ps1_minimize_guard_checks_window_visible():
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    guard = _minimize_guard_line(text)
    assert "IsWindowVisible" in guard, guard
    assert "IsIconic" in guard, guard


def test_edge_keeper_ps1_never_calls_sw_hide():
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    assert not re.search(r"ShowWindow\([^)]*,\s*0\s*\)", text)
