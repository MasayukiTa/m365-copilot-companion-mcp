"""The keeper must watch every companion window, on both dedicated profiles.

Two gaps found on 2026-08-10 while checking the taskbar report:

  * Find() returned on the FIRST matching window, so a second top-level window on the
    same Edge process was never minimized -- and one stray visible window is the whole
    complaint;
  * the process filter was hardcoded to 'copilot-companion-edge', so the interactive
    bridge Edge (copilot-bridge-edge, :9223) had no persistent watcher at all. It only
    stayed hidden because relay/edge_recover.py's rehide() happens to run on recovery.

Neither had surfaced yet: the fleet Edge currently owns no window (hwnd 0) and the
bridge window carries WS_EX_TOOLWINDOW from rehide(). They are latent, not theoretical.
"""
from pathlib import Path

KEEPER = Path(__file__).resolve().parents[1] / "scripts" / "win" / "edge_keeper.ps1"
SRC = KEEPER.read_text(encoding="utf-8")


def test_keeper_handles_every_window_not_just_the_first():
    assert "FindAll" in SRC, "全ウィンドウを返す関数になっていない"
    assert "foreach ($h in [K]::FindAll(" in SRC, "戻り値を1枚ずつ処理していない"
    assert "found = h; return false;" not in SRC, "最初の1枚で列挙を打ち切っている"


def test_keeper_is_not_hardcoded_to_the_fleet_profile():
    assert "-match 'copilot-companion-edge'" not in SRC, "fleet プロファイル決め打ちに戻っている"
    assert "$ProfileMarker" in SRC, "プロファイルを引数で受け取っていない"
    assert "copilot-bridge-edge" in SRC, "ブリッジ側プロファイルが既定に含まれていない"


def test_keeper_never_leaves_the_window_hidden():
    """SW_HIDE で終えないこと。

    最初に書いたときは「一度も使わない」で固定したが、それでは taskbar ボタンが残る。
    WS_EX_TOOLWINDOW を立てるには属性変更の一瞬だけ隠す必要があるので、守るべき
    不変条件は「隠したまま終えない」。隠しっぱなしにすると Edge がタブの描画を捨て、
    駆動中の CDP が TargetClosedError で落ちる。
    """
    import re as _re
    calls = _re.findall(r"ShowWindow\(\$h,\s*(\d+)\)", SRC)
    assert calls, "ShowWindow の呼び出しが無い"
    assert calls[-1] != "0", "最後が SW_HIDE のまま: %s" % calls
    for i, c in enumerate(calls):
        if c == "0":
            assert "6" in calls[i + 1:], "SW_HIDE の後に最小化へ戻していない: %s" % calls


def test_keeper_marks_the_window_out_of_the_taskbar():
    """常駐しているのはこのループだけ＝起動直後の窓に印を付けられる唯一の場所。"""
    assert "SetWindowLong" in SRC
    assert "0x80" in SRC
    assert "GetWindowLong" in SRC, "既に立っているかを見ずに毎回書き換えている"


def test_keeper_still_only_minimizes_a_visible_window():
    """不可視の窓を最小化すると Windows が WS_VISIBLE を立て、タスクバーに出る。"""
    assert "IsWindowVisible($h)" in SRC
    assert "-not [K]::IsIconic($h)" in SRC
