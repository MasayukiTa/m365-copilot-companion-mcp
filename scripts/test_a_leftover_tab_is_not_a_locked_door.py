# -*- coding: utf-8 -*-
"""開きっぱなしの残骸タブは、鍵のかかった扉ではない。

## オーナーの新PC、2026-09-23

`quickstart` が `Sign in to M365` の段で**永久に止まった**:

    M365 sign-in is needed (a sign-in page is open: https://login.live.com/Me.srf,
                            https://login.microsoftonline.c...)
    waiting for sign-in... (a sign-in page is open: https://login.live.com/Me.srf, ...
    waiting for sign-in... (a sign-in page is open: https://login.live.com/Me.srf, ...
    （以下、同じ行が延々と）

`doctor_summary.txt` は `bad=1 warn=0 unknown=0` — 赤はこの1行だけ。

## 機構

`state()` は **URL だけ**で壁を判定し、しかもその判定が M365 タブの判定より**先に短絡**する。
`LOGIN_RE` は `login\\.live\\.com` を含むので、**残骸タブが1枚あれば永久に `sign_in_needed`**。
サインインを済ませても残骸は消えないので、ループから出る道が無い。

## 答えは既にリポジトリにあった

`relay/relay_fleet.py` の残骸刈り取り機は、**この2つの URL を名指しで**書かれている:

    login.microsoftonline.com/savedusers と login.live.com/Me.srf
    -- four such tabs were sitting in the fleet Edge when this was written
    THE URL IS NOT THE TEST, THE PAGE STATE IS.

刈り取り機は閉じる前に `edge_auth.classify_page` にページ状態を聞く。
`ensure_m365_signin` は CDP の `/json`（URL と title だけ、DOM は無い）しか読まないので
ページに聞けない — **だから聞けないなりに、残骸を壁と呼ぶのをやめる。**

## 何を許し、何を許さないか

残骸は「壁がある」証拠ではない。**「サインイン済み」の証拠でもない。**
残骸しか無ければ `cannot_tell`。「人が要る」側から外すのは安全で、
「もう大丈夫」側に足すのは安全ではない。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ensure_m365_signin as m  # noqa: E402
from relay import edge_auth  # noqa: E402

#: The two tabs on the operator's screen, verbatim in shape.
RESIDUE = ("https://login.live.com/Me.srf?wa=wsignin1.0",
           "https://login.microsoftonline.com/savedusers?wreply=x")
APP = "https://m365.cloud.microsoft/chat"
WALL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?login_hint=a@b.com"


def _state(monkeypatch, urls):
    monkeypatch.setattr(m, "tabs", lambda port: [{"url": u} for u in urls])
    return m.state(9222)


def test_residue_alone_is_not_a_sign_in_wall(monkeypatch):
    """**報告された画面そのもの。**この2枚だけで `sign_in_needed` と言ってはいけない。"""
    ready, why = _state(monkeypatch, RESIDUE)
    assert ready is None, "residue is being reported as a locked door: %s" % why


def test_residue_alone_is_not_a_sign_in_either(monkeypatch):
    """**外した先が `True` だと、今度は逆向きに嘘をつく。**残骸は何も言っていない。"""
    ready, _ = _state(monkeypatch, RESIDUE)
    assert ready is not True


def test_residue_says_the_tabs_are_leftovers(monkeypatch):
    """`cannot_tell` の理由が「タブが無い」になると、2枚開いているブラウザを探しに行かせる。"""
    _, why = _state(monkeypatch, RESIDUE)
    assert "leftover" in why
    assert "login.live.com/Me.srf" in why


def test_the_app_tab_decides_once_the_residue_is_out_of_the_way(monkeypatch):
    """**これがループの出口。**`quickstart` は m365 のページを開いてから待つので、
    残骸を壁と数えなくなれば、開いたタブが落ち着いた時点で `signed_in` になる。"""
    ready, _ = _state(monkeypatch, list(RESIDUE) + [APP])
    assert ready is True


def test_a_real_wall_is_still_a_wall(monkeypatch):
    ready, why = _state(monkeypatch, [WALL])
    assert ready is False
    assert "login.microsoftonline.com/common/oauth2" in why


def test_a_real_wall_outranks_a_stale_app_tab(monkeypatch):
    """**安全側は変えない。**期限切れで古いアプリタブが残っていても、本物の壁が開いていれば
    それは人が要る状態。ここを緩めると、働く時刻に認証エラーで落ちる。"""
    ready, _ = _state(monkeypatch, [WALL, APP])
    assert ready is False


def test_the_wall_url_still_drops_its_query(monkeypatch):
    """`login_hint=` はメールアドレス。画面に出るし、スクリーンショットに写る。"""
    _, why = _state(monkeypatch, [WALL])
    assert "login_hint" not in why and "a@b.com" not in why and "?" not in why


@pytest.mark.parametrize("url,expected", [
    ("https://login.live.com/Me.srf?wa=wsignin1.0", True),
    ("https://login.microsoftonline.com/savedusers?wreply=x", True),
    ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize", False),
    ("https://m365.cloud.microsoft/chat", False),
    ("", False),
    (None, False),
])
def test_which_urls_are_leftovers(url, expected):
    assert edge_auth.looks_like_auth_bounce_residue(url) is expected


def test_the_two_shapes_are_defined_once():
    """**4つのビルド一覧で同じ日に払った授業料。**残骸の定義は `relay/edge_auth` にあり、
    チェッカーはそれを読む。ここにもう一組書けば、次に増えたとき片方だけが更新される。"""
    import ast
    import io
    path = os.path.join(REPO, "scripts", "ensure_m365_signin.py")
    src = io.open(path, encoding="utf-8").read()
    assert "edge_auth.looks_like_auth_bounce_residue" in src

    # PARSED, NOT MATCHED. The first version searched the raw text and failed on a COMMENT that
    # quotes the operator's screen -- the third source assertion today to catch its own spelling
    # rather than the property it names. A comment is not in the AST at all, and a docstring is
    # the one string constant that is prose by construction, so both drop out here and what is
    # left is a literal the program would actually use.
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            low = node.value.lower()
            for marker in edge_auth.AUTH_BOUNCE_RESIDUE_MARKERS:
                assert marker not in low, (
                    "%r is spelled out in the checker's code as well as in relay/edge_auth "
                    "(line %d) -- two places to update when a third shape turns up"
                    % (marker, node.lineno))


def test_the_countdown_line_stays_inside_a_console(monkeypatch):
    """**内容は正しかった。読めなかっただけ。**`\\r` は最後の *物理* 行の先頭に戻るので、
    コンソール幅を超えた行は折り返して消えず、毎回の更新が積み上がる — 報告された画面が
    同じ行の山になっていたのはそれ。理由は1回だけ別の行に出し、繰り返す行は短く保つ。"""
    import ast
    import io
    # AST AGAIN, AND FOR THE SAME REASON. Reading the source as text found the FIRST occurrence
    # of "waiting for sign-in...", which is the comment above the code quoting the operator's
    # screen -- so the assertion passed while the code it names was mutated back. That is the
    # fourth source assertion today to check its own spelling instead of the property.
    tree = ast.parse(io.open(os.path.join(REPO, "scripts", "ensure_m365_signin.py"),
                             encoding="utf-8").read())
    formats = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "waiting for sign-in" in n.value]
    assert len(formats) == 1, \
        "expected exactly one countdown format string in the code, found %d" % len(formats)
    fmt = formats[0]
    assert "%s" not in fmt, (
        "the redrawn line interpolates the reason (%r), so it grows past the console width and "
        "every tick leaves a wrapped copy behind -- the screen the operator sent" % fmt)
    assert len(fmt) < 70, "the redrawn line is %d characters and will wrap" % len(fmt)
