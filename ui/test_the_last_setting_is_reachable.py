# -*- coding: utf-8 -*-
"""設定パネルの最後の節に手が届くこと。

2026-09-14 の症状: 利用者が「設定にあった詳細設定がいつの間にか消えている」。
確認したことと、その結果:

| 調べたこと                                   | 結果             |
|---|---|
| ソースに節があるか                            | 有（26件のテストが検査済み） |
| exe のビルド時刻がソースより新しいか                | 新しい（16:23:01 対 16:05:10） |
| 走っているプロセスがその exe から起動したか          | はい（16:23:07） |
| exe の中に `詳細設定` の文字列があるか（UTF-16LE）  | **有る** |

つまり**作られていて、入っていて、届かなかった**。`BuildSettingsPanel` が返すのは
`card.Child = col`（素の StackPanel）で、節は増える一方なので、最後の節が画面外に出た。

同じ罠は同じファイルに既に書かれている。詳細設定**ポップアップ側**の ScrollViewer に
「a popup does not scroll on its own -- without this it grows past the screen and the buttons
at the bottom become unreachable」と。**子には入れて親には入れなかった。**

だからこのテストは「詳細設定が在るか」ではなく**「最後の節に届くか」**を検査する。
在ることは既に他のテストが見ており、それは今回の症状を捕まえられなかった。
"""
from __future__ import annotations

import io
import os
import re

SOURCE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FleetCockpit.cs")
SOURCE = io.open(SOURCE_PATH, encoding="utf-8", errors="replace").read()


def _body(start, end):
    i = SOURCE.index(start)
    j = SOURCE.index(end, i)
    return SOURCE[i:j]


def test_the_settings_panel_scrolls():
    """届かない節は、無い節と区別がつかない。"""
    body = _body("UIElement BuildSettingsPanel()", "\n    // ══")
    assert "ScrollViewer" in body, (
        "設定パネルに ScrollViewer が無い。節は増える一方なので、"
        "いちばん下の節が画面外に出たら操作できない")
    assert re.search(r"card\.Child\s*=\s*panelScroll", body), (
        "ScrollViewer は作られているが card に入っていない")


def test_its_height_is_taken_from_the_screen_not_from_a_constant():
    """固定ピクセルは、ある1つの表示倍率と1つのモニタでだけ正しい。

    このパネルは ui_scale で拡大縮小するので、620 のような定数を置くと
    倍率を上げた利用者のところで同じ症状が黙って戻る。
    """
    body = _body("UIElement BuildSettingsPanel()", "\n    // ══")
    assert "SystemParameters.WorkArea" in body, (
        "パネルの高さ上限が画面から取られていない。定数だと倍率を変えた瞬間に再発する")


def test_the_advanced_row_is_still_the_last_thing_in_the_panel():
    """この配置が症状の前提だったので、動いたら分かるようにしておく。

    最後で無くなること自体は悪くない。ただしそのときは、この症状が「最後の節」ではなく
    別の節で起きるという意味なので、テストの文面を直す必要がある。
    """
    body = _body("UIElement BuildSettingsPanel()", "\n    // ══")
    after = body[body.index("AdvancedSubmenuRow()"):]
    adds = [m for m in re.findall(r"col\.Children\.Add\(", after)]
    assert not adds, (
        "詳細設定の後ろに節が増えている（%d 件）。このテストの前提が古い" % len(adds))


def test_the_child_popup_keeps_its_own_scroller():
    """親に付けたからといって子から外さないこと。中のリストは行数が読めない。"""
    body = _body("void ToggleAdvancedPopup(UIElement anchor)", "\n    void SetRetDays")
    assert "ScrollViewer" in body
