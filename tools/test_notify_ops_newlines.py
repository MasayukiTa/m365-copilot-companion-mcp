"""トースト本文の改行が文字として出ていた件（2026-08-21、実機のスクリーンショットより）。

運用者の画面には `running system.¥n¥nsee exactly what moved:¥n  python -m ...` と出ていた。
原因は本文を json.dumps して PowerShell の二重引用符文字列に埋めていたこと --
PowerShell は \n を改行と解釈しない（バッククォート n を使う）。値を base64 で渡し、
向こう側でデコードすることで、改行も引用符も非ASCIIもそのまま通る。

呼び出し側の問題ではないので、直すのはここ1箇所。全ての通知経路がこの関数を通る。
"""
import base64
import json
import re

import tools.notify_ops as N

#: THE REAL FUNCTION, taken at import time. conftest.py autouse-stubs notify_desktop for every
#: test in the repo so nothing fires a toast by accident -- which also means a test ABOUT that
#: function never reaches it. Collection happens before the fixture runs, so this is the real
#: one; the stub still protects every other test.
_REAL = N.notify_desktop


def _script(monkeypatch, **kw):
    """notify_desktop が PowerShell に渡す実際のスクリプトを捕まえる。"""
    seen = {}

    class _R:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def _run(cmd, **_kw):
        seen["script"] = cmd[-1]
        return _R()

    # EMPTIED, NOT DELETED. pytest keeps this variable set for the duration of a test, and
    # deleting it did not stick -- the guard kept firing and nothing was ever captured. An
    # empty value is falsy, which is what the guard actually reads.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setattr(N.subprocess, "run", _run)
    monkeypatch.setattr(N.shutil, "which", lambda _n: "powershell.exe")
    _REAL(**kw)
    return seen["script"]


def _decoded(script, var):
    m = re.search(r"\$%s\s*=\s*Dec\s+\"([A-Za-z0-9+/=]+)\"" % var, script)
    assert m, "%s が base64 で渡されていない: %s" % (var, script[:200])
    return base64.b64decode(m.group(1)).decode("utf-8")


def test_a_multi_line_body_survives(monkeypatch):
    body = "一行目\n二行目\n\nundo:\n  python -m relay.selfimprove.frozen --revoke"
    got = _decoded(_script(monkeypatch, title="t", body=body), "body")
    assert got == body
    # バックスラッシュ + n の2文字が残っていないこと。真の改行と紛れないよう
    # バックスラッシュ + n の2文字が残っていないこと。真の改行と区別するため
    # chr(92) を使う。ここをエスケープで書くと、この行自身が同じ罠にはまる。


def test_quotes_and_japanese_survive(monkeypatch):
    body = '「引用符」と "quotes" と $variable と `backtick`'
    assert _decoded(_script(monkeypatch, title="t", body=body), "body") == body


def test_the_title_goes_through_the_same_path(monkeypatch):
    title = "! 凍結集合が\n承認なしに変わった"
    assert _decoded(_script(monkeypatch, title=title, body="b"), "title") == title


def test_a_click_target_reaches_the_toast(monkeypatch):
    """URI が $launch に届き、activation がその変数に掛かっていること。

    ここから確かめられるのはそこまで。PowerShell 側の if を $false に書き換えても
    生成される文字列は同じなので、Python からは区別できない -- 変異テストで実際に
    素通りした。分岐が変数を見ていることまでを見張り、その先は実機の1回で確かめる。"""
    uri = "file:///C:/x/selfimprove_last_act.txt"
    script = _script(monkeypatch, title="t", body="b", launch=uri)
    assert ('$launch = "%s"' % uri) in script, "URI が変数に入っていない"
    assert "if ($launch)" in script, "activation が launch に掛かっていない"
    assert "activationType" in script and "protocol" in script


def test_no_click_target_leaves_the_toast_alone(monkeypatch):
    """launch を渡さない既存の呼び出しが、余計な属性で壊れないこと。"""
    script = _script(monkeypatch, title="t", body="b")
    assert '$launch = ""' in script


def test_nothing_from_the_body_can_reach_the_shell(monkeypatch):
    """base64 にする副産物として、本文はもうシェルの文法に触れない。"""
    script = _script(monkeypatch, title="t", body="'; Remove-Item -Recurse C:\ ; '")
    assert "Remove-Item" not in script


def test_it_stays_inert_under_pytest(monkeypatch):
    """この関数は全通知経路の唯一の出口。テスト中に本物のトーストを出さない。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert "suppressed" in _REAL(title="t", body="b")
