"""In-process 呼び出しは unlock ゲートを満たせない -- 満たせと言うのが誤り。

実測 2026-08-21: relay は毎ターン `memory_save` を呼び、ゲートが返すロック文字列を捨てていた。
つまり cross-session history は**一度も書かれていなかった**。さらにその拒否1回ごとに
識別子なしの拒否スロットが更新され、並行する艦隊ワーカーが他人の拒否を自分のロックと
読み違えて自動 unlock の試行を消費していた。

ゲートが答えられる問いは「このHTTPリクエストの背後にいる遠隔の呼び出し元が、自分のIPに
対するパスワード所持を証明したか」。リクエストが無い呼び出し元について、この問いは
答えようがない -- unlock() 自身も get_http_request() を要るので、逃げ道が存在しない。
"""
import pytest

from tools import memory_ops


def test_the_gated_entry_point_still_gates(monkeypatch):
    """公開されているツール面は変わらない。緩めたのは in-process 用の別関数だけ。"""
    monkeypatch.setattr(memory_ops, "require_unlocked",
                        lambda: "[locked: no HTTP request context] Call unlock(...) first.")
    out = memory_ops.memory_save("k", "v", scope="test_scope")
    assert out.startswith("[locked")


def test_an_in_process_caller_can_actually_save(monkeypatch, tmp_path):
    """ここが通らないと、relay の履歴は今日までと同じく黙って消え続ける。"""
    monkeypatch.setattr(memory_ops, "require_unlocked",
                        lambda: pytest.fail("local の保存がゲートを呼んでいる"))
    out = memory_ops.memory_save_local("relay.test.turn1", "本文", scope="test_scope",
                                       tags=["relay"])
    assert not out.startswith("[locked")
    assert "relay.test.turn1" in out or "saved" in out.lower()


def test_the_local_variant_is_not_exposed_as_a_tool():
    """緩い方が道具として外に出ていたら、それはゲートを外したのと同じ。"""
    import io
    import re
    src = io.open("main.py", encoding="utf-8").read()
    assert "memory_save_local" not in src, "main.py が local 版を公開している"


def test_the_gated_and_local_paths_write_the_same_way(monkeypatch):
    """片方だけが別の形で書くと、履歴が2種類になる。"""
    monkeypatch.setattr(memory_ops, "require_unlocked", lambda: None)
    a = memory_ops.memory_save("k.same", "v", scope="test_scope")
    b = memory_ops.memory_save_local("k.same2", "v", scope="test_scope")
    assert a.split("k.same")[0] == b.split("k.same2")[0]


def test_the_relay_imports_the_local_variant():
    """import を戻すと、症状は静かに再発する -- 戻り値を捨てているので誰も気づかない。"""
    import io
    src = io.open("relay/copilot_autopilot_relay.py", encoding="utf-8").read()
    assert "memory_save_local as memory_save" in src
    assert "import memory_load, memory_save" not in src
