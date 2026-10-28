# -*- coding: utf-8 -*-
"""一覧は常に一番下まで届き、履歴を全部消してもスクロールできること。

利用者の報告（2件）:
 1. 一覧を一番下までスクロールしきれない（だいぶ前から）。
 2. 履歴・完了・走行中が全部ある状態で「履歴を全部消す」と、スクロールが不可能になる。

この exe は WPF なので、固定はソース文字列で行う（生きた WPF は走らせない）。
本当の原因は 1 つのレイアウト: `_list` が `listWrap` の中にありながら
`_list.MaxHeight <- listWrap.ActualHeight` と自分自身の高さに縛られていた（循環）。
Auto の行は無限高さで測るので、仮想化 OFF の全行が実体化され、上限は1パス
遅れて決まる。履歴を消して大きく行が減った直後、viewport == content に落ち着き
スクローラーが死ぬ（= 報告 2）、そうならないときも extent が短く最後の行に
届かない（= 報告 1）。固定 '*' 行いっぱいにリストを広げれば（Stretch）、高さが自分の内容に
依存せず、端は常に到達可能になる。
"""
from __future__ import annotations

import re

from tools import cockpit_source  # reads every part of the split cockpit source

SOURCE = cockpit_source.read(encoding="utf-8", errors="replace")


def _member(signature):
    return cockpit_source.member(SOURCE, signature)


def test_clear_history_never_disables_the_list_scroller():
    """(a) 履歴を消す処理が、一覧の ScrollViewer を無効にしないこと。

    消した直後に残るのは走行中・完了カードで、それらを最後まで読めなければ
    いけない。このパスは Disabled にしてはいけず、Auto を再度確かめる。
    """
    body = _member("void ClearHistory()")
    assert "ScrollBarVisibility.Disabled" not in body, (
        "ClearHistory が一覧のスクロールを無効にしている。履歴を消したあとも"
        "走行中・完了カードはスクロールできなければならない")
    assert re.search(
        r"SetVerticalScrollBarVisibility\(\s*_list\s*,\s*ScrollBarVisibility\.Auto\s*\)",
        body), (
        "ClearHistory が一覧のスクロールを Auto に再度確定していない。"
        "消去パスはスクロールを復元することはあっても、止めてはならない")


def test_the_list_is_bottom_anchored_without_a_self_referential_max_height():
    """循環した MaxHeight 自己縛りを残さないこと。

    _list が listWrap の中にありながら listWrap.ActualHeight に MaxHeight を縛ると、
    Auto 行と 仮想化 OFF の組み合わせで viewport が1パス遅れて決まり、履歴を
    消した直後にスクロールが死ぬ。固定 '*' 行と下揃えで置き換える。
    """
    body = _member("void BuildChrome()")
    assert "listWrap" in body, "ボトムアンカーのラッパが見当たらない"
    # The list must NOT be capped by a binding to its own wrapper's height (the circular pattern).
    assert not re.search(
        r"maxBind\.Source\s*=\s*listWrap", body), (
        "_list の MaxHeight が listWrap.ActualHeight に縛られている（循環）。"
        "履歴を消した直後に viewport==content でスクロールが止まる原因")
    assert not re.search(
        r"_list\.SetBinding\(\s*FrameworkElement\.MaxHeightProperty", body), (
        "_list に MaxHeight バインディングが残っている。固定 '*' 行で高さを決める")
    # TALL CASE: the list must FILL the fixed '*' row (Stretch), not bottom-anchor inside it.
    # A non-virtualizing ListBox reports its full content height as its desired size; a
    # Bottom-aligned child taller than the row is arranged at that desired height and pinned
    # to the row's bottom edge, so its top overflows above the row and the ListBox's own
    # ScrollViewer takes content-height as its viewport -- viewport and extent disagree and the
    # last row is pushed under the composer (the "履歴の一番下が欠ける" regression). Stretch pins the
    # list to the row so viewport == row height and extent == content height: the end is
    # always reachable.
    assert re.search(
        r"_list\.VerticalAlignment\s*=\s*VerticalAlignment\.Stretch", body), (
        "_list が Stretch になっていない。固定 '*' 行にリストを密着させ、"
        "viewport == 行高 / extent == 内容高 にすることで最下部まで届く")
    # And it must NOT bottom-anchor inside that row (the pattern that caused the clip).
    assert not re.search(
        r"_list\.VerticalAlignment\s*=\s*VerticalAlignment\.Bottom", body), (
        "_list が固定 '*' 行の中で下揃えになっている。"
        "内容が行より高いとき上端がはみ出し、最下部が欠ける")


def test_the_last_row_has_breathing_room_above_the_input_area():
    """(b) 末尾の余白が、入力の領域と重ならないこと。

    リストの下パディングが、底に浮くコンポーザ（影付き）の上に最後の行の余白を
    確保する。小さすぎる値だと最後の行が影に潜るので、十分な値を要求する。
    """
    body = _member("void BuildChrome()")
    m = re.search(
        r"_list\.Padding\s*=\s*new Thickness\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(\d+)\s*\)",
        body)
    assert m, "_list.Padding が見当たらない"
    bottom = int(m.group(1))
    assert bottom >= 24, (
        "リストの下パディングが %d しかない。最後の行がコンポーザの影に潜らない"
        "よう 24 以上の余白を置く" % bottom)
