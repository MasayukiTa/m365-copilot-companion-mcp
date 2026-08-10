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


def test_rehide_ps_never_leaves_the_window_hidden():
    """SW_HIDE の状態で終えないこと。

    元は「SW_HIDE を一度も書かない」と固定していた。いまは WS_EX_TOOLWINDOW を
    立てるためにその一瞬だけ使う（属性変更は窓を隠してからでないと効かない）ので、
    禁じるべきは「使うこと」ではなく「隠したまま終えること」。隠したままにすると
    Edge がタブの描画を捨て、駆動中に TargetClosedError で落ちる。
    """
    calls = re.findall(r"ShowWindow\(\$h,\s*(\d+)\)", _REHIDE_PS)
    assert calls, "ShowWindow の呼び出しが無い"
    assert calls[-1] != "0", "最後が SW_HIDE のまま: %s" % calls
    # 隠した直後に必ず戻していること（隠しっぱなしの経路を作らない）
    for i, c in enumerate(calls):
        if c == "0":
            assert "6" in calls[i + 1:], "SW_HIDE の後に最小化へ戻していない: %s" % calls


def test_edge_keeper_ps1_declares_is_window_visible_pinvoke():
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    assert 'extern bool IsWindowVisible(IntPtr h);' in text


def test_edge_keeper_ps1_minimize_guard_checks_window_visible():
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    guard = _minimize_guard_line(text)
    assert "IsWindowVisible" in guard, guard
    assert "IsIconic" in guard, guard


def test_edge_keeper_ps1_never_leaves_the_window_hidden():
    """SW_HIDE の状態で終えないこと。

    _REHIDE_PS と同じ基準に揃えた。元は「keeper は SW_HIDE を一度も書かない」と
    固定していたが、最小化だけでは taskbar ボタンが残り、利用者が見るのはそれ。
    WS_EX_TOOLWINDOW を立てるには属性変更の一瞬だけ窓を隠す必要があるので、
    禁じるべきは「使うこと」ではなく「隠したまま終えること」。隠したままにすると
    Edge がタブの描画を捨て、駆動中に TargetClosedError で落ちる。
    """
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    calls = re.findall(r"ShowWindow\(\$h,\s*(\d+)\)", text)
    assert calls, "ShowWindow の呼び出しが無い"
    assert calls[-1] != "0", "最後が SW_HIDE のまま: %s" % calls
    for i, c in enumerate(calls):
        if c == "0":
            assert "6" in calls[i + 1:], "SW_HIDE の後に最小化へ戻していない: %s" % calls


def test_edge_keeper_ps1_removes_the_taskbar_button_not_just_minimizes():
    """常駐しているのはこのループだけ。起動直後の窓に印を付けられるのもここだけ。

    rehide() は復旧時にしか走らないので、再起動のたびに窓がタスクバーへ戻っていた
    （2026-08-10 に2回報告）。
    """
    text = EDGE_KEEPER_PS1.read_text(encoding="utf-8")
    assert "SetWindowLong" in text, "WS_EX_TOOLWINDOW を立てていない"
    assert "0x80" in text
    assert "GetWindowLong" in text, "既に立っているかを見ずに毎回書き換えている"
