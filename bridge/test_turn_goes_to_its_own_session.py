"""あるセッション宛に積まれたターンが、別の会話に送られないこと。

## 何が起きていたか

`_run_one_turn(sid, msg)` は `sid` を受け取るが、実際の送信は
`_send_and_stream_once` → 現在の driver → **いま開いている会話**に対して行われる。
その会話は `ACTIVE_SID` のものであって、`sid` のものとは限らない。両者を突合する処理は無かった。

理論上の話ではなく届く経路がある:

- `/send?sid=X` は **クエリ文字列から X をそのまま受け取り**、X の pending に積む
  (`copilot_bridge.py` の /send ハンドラ)
- `session_cli` の goal ループは `list_sessions()[0]` の sid を渡す。これは `ACTIVE_SID` とは
  **別の規則**で選ばれる(`ACTIVE_SID` は起動時 auto-resume が `conv_url` 非空で絞って決める)

食い違ったとき、メッセージは**ある会話で発話され、別の会話のトランスクリプトに記録される**。
どちらを読んだ人も、実際には行われていないやり取りを読むことになる。

## なぜ「送らない」が正しいか

送信が不可逆な側だから。積み直せばメッセージは宛先のセッションを待てるが、
一度別の会話に送ってしまうと取り消せない。
"""
import tempfile

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    import importlib

    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, tempfile.mkdtemp())
    importlib.reload(S)

    import bridge.copilot_bridge as B
    monkeypatch.setattr(B, "S", S, raising=False)
    return B, S


def _turner(B, monkeypatch, sent):
    """`_run_one_turn` だけを呼べる入れ物。実際に送られたら sent に積む。"""
    class _H(object):
        def _send_and_stream_once(self, payload, stream_out=True):
            sent.append(payload)
            return "answer"

    h = _H()
    h._run_one_turn = B.Handler._run_one_turn.__get__(h, _H)
    monkeypatch.setattr(B, "_prepare_capture_baseline", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_bridge_unlock_password", lambda *a, **k: "", raising=False)
    return h


def test_a_turn_for_another_session_is_not_sent(bridge, monkeypatch):
    """宛先が active でないなら、**送らない**。ここが本題。"""
    B, S = bridge
    a = S.new_session("A")["sid"]
    b = S.new_session("B")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    sent = []
    h = _turner(B, monkeypatch, sent)
    out = h._run_one_turn(b, "count the widgets", stream_out=False)

    assert sent == [], "別のセッション宛のターンを送ってしまった: %r" % (sent,)
    assert isinstance(out, dict) and out.get("wrong_session"), (
        "拒否したことを呼び出し元が判別できない: %r" % (out,))
    assert out.get("queued_for") == b and out.get("active") == a


def test_a_turn_for_the_active_session_is_sent(bridge, monkeypatch):
    """一致していれば普通に送ること。ガードが全部止めては意味が無い。"""
    B, S = bridge
    a = S.new_session("A")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    sent = []
    h = _turner(B, monkeypatch, sent)
    out = h._run_one_turn(a, "count the widgets", stream_out=False)

    assert sent == ["count the widgets"], "一致しているのに送られていない"
    assert out == "answer"


def test_no_active_session_is_not_treated_as_a_mismatch(bridge, monkeypatch):
    """`ACTIVE_SID` が無い状態(起動直後など)を「不一致」と読まないこと。

    「まだ決まっていない」と「別のものに決まっている」は別のこと。
    前者で止めると、最初のターンが永久に出せなくなる。
    """
    B, S = bridge
    a = S.new_session("A")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", None, raising=False)

    sent = []
    h = _turner(B, monkeypatch, sent)
    h._run_one_turn(a, "hello", stream_out=False)
    assert sent == ["hello"], "ACTIVE_SID が未設定なだけで送信を止めている"


def test_a_refused_item_goes_back_on_the_queue(bridge, monkeypatch):
    """拒否したメッセージは失われないこと。

    ドレインは既に pop している。積み直さなければ「送られず、記録もされず、消える」になる。
    """
    B, S = bridge
    a = S.new_session("A")["sid"]
    b = S.new_session("B")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)
    B._queue_input_locked(b, "first")
    B._queue_input_locked(b, "second")

    sent = []

    class _H(object):
        def _send_and_stream_once(self, payload, stream_out=True):
            sent.append(payload)
            return "answer"

    h = _H()
    h._run_one_turn = B.Handler._run_one_turn.__get__(h, _H)
    h._drain_pending_queue = B.Handler._drain_pending_queue.__get__(h, _H)
    monkeypatch.setattr(B, "_prepare_capture_baseline", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_bridge_unlock_password", lambda *a, **k: "", raising=False)

    h._drain_pending_queue(b)

    assert sent == [], "拒否したはずのメッセージが送られている"
    pending = (S.load(b) or {}).get("pending") or []
    assert "first" in pending and "second" in pending, (
        "拒否したメッセージがキューから消えた: %r" % (pending,))
