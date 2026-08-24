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

import pytest

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


# ---- ダッシュボードを開く経路（2026-08-21） ---------------------------------------------------

def test_a_host_without_the_built_ui_gets_nothing_and_no_exception(monkeypatch, tmp_path):
    """サーバだけを動かすホストには EXE が無い。それは異常ではなく通常の状態で、
    そこでは呼び出し側が書面の説明に落ちる。例外を投げたら通知ごと落ちる。"""
    from pathlib import Path
    # 記録して後で表明する。関数は except Exception で全部飲むので、中で raise しても
    # 握り潰されて「通った」ように見える -- 実際に変異テストが素通りしてそれが分かった。
    called = []
    monkeypatch.setattr(N, "COCKPIT", Path(str(tmp_path / "not-built.exe")))
    monkeypatch.setattr(N.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert N.open_authority_dashboard() == ""
    assert called == [], "存在しない EXE を起動しようとした"


def test_a_built_ui_is_launched_with_the_authority_switch(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)   # see _fake_cockpit
    exe = tmp_path / "FleetCockpit.exe"
    exe.write_text("", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(N, "COCKPIT", Path(str(exe)))
    monkeypatch.setattr(N.subprocess, "Popen", lambda cmd, **k: seen.update(cmd=cmd))
    out = N.open_authority_dashboard()
    assert out.endswith("FleetCockpit.exe")
    assert seen["cmd"][1] == "--authority"


def test_a_launch_that_fails_is_not_fatal(monkeypatch, tmp_path):
    from pathlib import Path
    exe = tmp_path / "FleetCockpit.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(N, "COCKPIT", Path(str(exe)))

    def boom(*a, **k):
        raise OSError("no window station")

    monkeypatch.setattr(N.subprocess, "Popen", boom)
    assert N.open_authority_dashboard() == ""


@pytest.fixture(autouse=True)
def _no_real_desktop_state(monkeypatch):
    """このファイルのテストは、この端末で何が走っているかについてのものではない。

    `open_authority_dashboard` が「既に窓が開いていれば開かない」を見るようになった時点で、
    実機に FleetCockpit.exe が走っていると既存テストまで落ちる -- 実際、再起動後に2つ
    走っていて2件落ちた。世界に触る前提条件は、世界についてでないテストではスタブする。
    ここを見るテストは自分で上書きする。
    """
    from tools import notify_ops as _N
    _N._DASHBOARD_LAST[0] = 0.0
    monkeypatch.setattr(_N, "_dashboard_already_up", lambda: "")


# ---- 通知がウィンドウを無制限に生めないこと --------------------------------------------------------

#: The real guard, captured at import time before any fixture stubs it out. The tests that
#: ARE about the guard put it back; the rest run without the machine's actual process list.
_REAL_GUARD = N._dashboard_already_up


def _fake_cockpit(tmp_path, monkeypatch):
    # open_authority_dashboard は notify_desktop と同じく PYTEST_CURRENT_TEST で不活性化する
    # ようになった(操作者の実デスクトップに窓を出さないため)。この一連のテストはその関数
    # 自体を試すので、tools/test_approval_policy.py と同じ作法で環境変数を外す。
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    exe = tmp_path / "FleetCockpit.exe"
    exe.write_text("", encoding="utf-8")
    from tools import notify_ops as N
    monkeypatch.setattr(N, "COCKPIT", exe)
    opened = []
    monkeypatch.setattr(N.subprocess, "Popen",
                        lambda *a, **k: opened.append(a) or type("P", (), {})())
    N._DASHBOARD_LAST[0] = 0.0
    return N, opened


def test_a_burst_of_events_opens_one_window(tmp_path, monkeypatch):
    """通知1件ごとに1枚開き、冷却も既存窓の確認も無かった。
    通知対象の行為が1日に42件着地し(再署名23・不一致19)、
    マシンは午後じゅう WPF の窓を42枚開き続け、
    Claude が落ち、最後に PC ごと落ちた。

    どちらの半分も単独では致命ではない -- 行為が多すぎたことと、
    バーストしない負荷でしか生き延びない起動器と。"""
    N, opened = _fake_cockpit(tmp_path, monkeypatch)
    # 本物のガードを戻し、プロセス確認だけ黙らせる -- 冷却の側を試験したいので。
    monkeypatch.setattr(N, "_dashboard_already_up", _REAL_GUARD)
    monkeypatch.setattr(N, "cockpit_running", lambda: False)
    for _ in range(42):
        N.open_authority_dashboard()
    assert len(opened) == 1, len(opened)


def test_the_cooldown_alone_stops_a_burst(tmp_path, monkeypatch):
    """走っているプロセスの確認だけでは、最初の1つが一覧に出る前に
    全部発火するバーストを止められない。"""
    N, opened = _fake_cockpit(tmp_path, monkeypatch)
    monkeypatch.setattr(N, "_dashboard_already_up", _REAL_GUARD)
    monkeypatch.setattr(N, "cockpit_running", lambda: False)
    for _ in range(10):
        N.open_authority_dashboard()
    assert len(opened) == 1


def test_a_window_left_open_from_yesterday_is_noticed(tmp_path, monkeypatch):
    """冷却だけでは、昨日から開きっぱなしの窓に気づけない。"""
    N, opened = _fake_cockpit(tmp_path, monkeypatch)
    N._DASHBOARD_LAST[0] = 0.0
    monkeypatch.setattr(N, "_dashboard_already_up", lambda: "already running")
    assert N.open_authority_dashboard() == ""
    assert opened == []


def test_the_first_notification_still_opens_the_control(tmp_path, monkeypatch):
    """上限を足したせいで、運用者を制御画面に連れて行く本来の目的まで
    潰していないこと。"""
    N, opened = _fake_cockpit(tmp_path, monkeypatch)
    monkeypatch.setattr(N, "_dashboard_already_up", lambda: "")
    assert N.open_authority_dashboard() != ""
    assert len(opened) == 1


def test_a_running_cockpit_is_seen_without_the_cooldown(tmp_path, monkeypatch):
    """冷却が切れていても、開いている窓があればもう1枚開かない。"""
    N, opened = _fake_cockpit(tmp_path, monkeypatch)
    monkeypatch.setattr(N, "_dashboard_already_up", _REAL_GUARD)
    monkeypatch.setattr(N, "cockpit_running", lambda: True)
    N._DASHBOARD_LAST[0] = 0.0
    assert N.open_authority_dashboard() == ""
    assert opened == []


def test_the_two_halves_are_testable_apart():
    """冷却は算術、プロセス確認は世界を読む。混ぜると、冷却のテストが
    『運用者がダッシュボードを開いているか』で通ったり落ちたりする --
    実際そうなって2件落ちた。"""
    # autouse フィクスチャがラムダに差し替えているので、本物を見る。
    import inspect
    src = inspect.getsource(_REAL_GUARD)
    assert "cockpit_running()" in src
    assert "psutil" not in src
