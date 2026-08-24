"""認証バウンスの残骸タブを掃除する。ただしサインインを待っているページは閉じない。

掃除役は前から存在し、30スイープごとに走っていたが、この端末に実際に溜まる残骸を
1つも掴めていなかった -- `looks_like_redirect_landing` は CsrToSSR のクエリ標識しか
見ないので、login.microsoftonline.com/savedusers と login.live.com/Me.srf は候補に
すらならない。書いた時点でフリート Edge にその種のタブが4枚あった。
"""
import ast
import inspect

import pytest

from relay import relay_fleet as RF


@pytest.mark.parametrize("url,expected", [
    ("https://m365.cloud.microsoft/chat", False),
    ("https://login.microsoftonline.com/savedusers?wreply=x", True),
    ("https://login.live.com/Me.srf?wa=wsignin1.0", True),
    ("https://login.windows.net/common/oauth2/authorize", True),
    ("", False),
    (None, False),
])
def test_which_urls_are_auth_hosts(url, expected):
    assert RF._on_signin_host(url) is expected


def test_the_old_matcher_still_misses_them_which_is_why_this_exists():
    """回帰の目印。ここが True に変わったなら、掃除役は別の理由で動いている。"""
    assert RF.looks_like_redirect_landing(
        "https://login.microsoftonline.com/savedusers?wreply=x") is False


def test_a_page_asking_for_a_sign_in_is_never_closed():
    """URL ではなくページ状態で判定する。edge_auth がその為に存在する。
    サインイン中のタブを閉じると、人間が操作できる唯一の面を奪う。"""
    src = inspect.getsource(RF._reap_orphan_redirect_tabs)
    assert "_on_signin_host(u)" in src
    assert "edge_auth" in src
    assert 'cls == "needs_signin"' in src
    # 判定できなかったときは閉じない
    i = src.index("except Exception:\n                    continue")
    j = src.index('if cls == "needs_signin"')
    assert i < j, "分類に失敗したときに閉じる側へ倒れている"


def test_the_reaper_never_raises():
    """スイープの中から呼ばれる。ここで例外を出すと走行ごと落ちる。"""
    tree = ast.parse(inspect.getsource(RF._reap_orphan_redirect_tabs).lstrip()).body[0]
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    RF._reap_orphan_redirect_tabs(None, [])      # 壊れた入力でも黙って戻る
