"""`/switch` がページと一緒に **セッション** も動かすこと。

## 何が起きていたか

`_do_switch` は `_goto_settled(url)` でページを動かすだけで、`ACTIVE_SID` を一度も
代入していなかった。しかも自身のコメントは
「Switching moves PAGE **and ACTIVE_SID**」と書いていた — それは `/new` と `/resume` の話で、
このハンドラは該当しない。

そしてこれは **UI の主経路** である: cockpit のチャットは会話をクリックしたときと、
会話へ送信する直前に `/switch` を呼ぶ。`/resume?sid=` は UI から一度も呼ばれない。
つまり「会話Bを開いて入力する」と、ページはB・`ACTIVE_SID`はAのままで、
ターンは **Aのトランスクリプトに記録されていた**。

気づけるはずだった唯一の検査(`_persist_exchange` がペインの guid と保存済みを突合)は、
ページ解放が既定ONのとき `_current_row_guid()` が "" を返すので **常に無言** になる。

## このテストの見かた

ソースの形ではなく、**ハンドラを実際に呼んで `ACTIVE_SID` がどうなったか**で見る。
"""
import os
import tempfile

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    """本物の session_store を空ディレクトリで持たせた copilot_bridge。"""
    import importlib

    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, tempfile.mkdtemp())
    importlib.reload(S)

    import bridge.copilot_bridge as B
    monkeypatch.setattr(B, "S", S, raising=False)
    return B, S


class _Parsed(object):
    def __init__(self, query):
        self.query = query


def _handler(B, monkeypatch, goto_ok=True):
    """`_do_switch` だけを呼べる最小の入れ物。ブラウザには触らない。"""
    monkeypatch.setattr(B, "release_socket_driver", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_reap_orphan_tabs", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_goto_settled", lambda *a, **k: goto_ok, raising=False)

    class _Page(object):
        url = "https://m365.cloud.microsoft/chat/"
    monkeypatch.setattr(B, "PAGE", _Page(), raising=False)

    class _H(object):
        def __init__(self):
            self.payload = None

        def _json(self, obj):
            self.payload = obj

    h = _H()
    h._do_switch = B.Handler._do_switch.__get__(h, _H)
    return h


CONV = ("https://m365.cloud.microsoft/chat/conversation/"
        "d870f6cd-4aa5-4d42-9626-ab690c041a1a?titleId=T_x")


def test_switching_to_a_known_conversation_moves_the_active_session(bridge, monkeypatch):
    """会話Bに対応する行があれば、ACTIVE_SID がそれになること。ここが本題。"""
    B, S = bridge
    a = S.new_session("A")["sid"]
    b = S.new_session("B")["sid"]
    S.touch(b, conv_url=CONV)
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    h = _handler(B, monkeypatch)
    h._do_switch(_Parsed("url=" + CONV.replace(":", "%3A").replace("/", "%2F")))

    assert B.ACTIVE_SID == b, (
        "ページは会話Bへ動いたのに ACTIVE_SID が %r のまま -- 次のターンは別の"
        "トランスクリプトに記録される" % B.ACTIVE_SID)
    assert h.payload.get("sid") == b, "どのセッションになったかを返していない"


def test_a_failed_navigation_does_not_move_the_session(bridge, monkeypatch):
    """ページが動かなかったなら、セッションも動かさないこと。

    開けなかった会話に ACTIVE_SID を向けるのは、同じ欠陥の向きを変えただけ。
    """
    B, S = bridge
    a = S.new_session("A")["sid"]
    b = S.new_session("B")["sid"]
    S.touch(b, conv_url=CONV)
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    h = _handler(B, monkeypatch, goto_ok=False)
    h._do_switch(_Parsed("url=" + CONV.replace(":", "%3A").replace("/", "%2F")))

    assert B.ACTIVE_SID == a, "開けなかった会話へ ACTIVE_SID を向けてしまった"


def test_an_unknown_conversation_is_reported_not_passed_over(bridge, monkeypatch, caplog):
    """対応する行が無いときは、黙って続けないこと。

    ここで勝手に行を作ると、任意の URL からセッションが増える(それは /adopt の仕事)。
    しかし無言で続けると、次のターンは前のセッションに入り、誰にも分からない。
    """
    import logging

    B, S = bridge
    a = S.new_session("A")["sid"]
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    said = []
    monkeypatch.setattr(B.logger, "warning",
                        lambda msg, *args, **kw: said.append(str(msg)), raising=False)

    h = _handler(B, monkeypatch)
    h._do_switch(_Parsed("url=" + CONV.replace(":", "%3A").replace("/", "%2F")))

    assert B.ACTIVE_SID == a, "行が無いのにセッションを動かした"
    assert any("no session row" in m for m in said), (
        "行が見つからなかったことを記録していない -- 無言で前のセッションに書き続ける")


def test_the_socket_shape_of_a_stored_reference_is_also_matched(bridge, monkeypatch):
    """ソケットが書く `sess:<guid>` 形式で保存された行も見つかること。

    いま会話の多くはこの形で保存されるので、ここが外れると主要な経路で
    「行が無い」に落ちる。
    """
    B, S = bridge
    a = S.new_session("A")["sid"]
    b = S.new_session("B")["sid"]
    S.touch(b, conv_url=B.make_sessref("d870f6cd-4aa5-4d42-9626-ab690c041a1a"))
    monkeypatch.setattr(B, "ACTIVE_SID", a, raising=False)

    h = _handler(B, monkeypatch)
    h._do_switch(_Parsed("url=" + CONV.replace(":", "%3A").replace("/", "%2F")))

    assert B.ACTIVE_SID == b, "sess:<guid> で保存された会話に切り替われない"
