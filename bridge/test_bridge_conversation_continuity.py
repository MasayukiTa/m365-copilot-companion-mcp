"""ブリッジのソケットが、見ている会話を継続すること。

## 何が起きていたか

`_bridge_socket_driver` は会話 ID をこう読んでいた:

    sess = S.get(ACTIVE_SID) if ACTIVE_SID else None     # ← S.get は存在しない
    ...
    except Exception:
        conv_id = ""                                      # ← AttributeError がここへ落ちる

`bridge.session_store` が公開しているのは `load` だけで `get` は無い(同じファイルの他7箇所は
正しく `S.load` を呼んでいる)。したがってこの読み取りは**毎回** AttributeError を出し、
裸の except がそれを「会話 ID が空」に変換していた。

`socket_route.driver_for` は空の会話 ID を「新しい会話を始めよ」と読む。つまり
**ソケット driver を作り直すたびに、Copilot 側で真新しいチャットが始まっていた** ——
このメソッドの docstring が約束していることの正反対:

    "so a bridge that reconnects continues the chat the user is looking at
     rather than silently starting a new one."

## このテストが見張るもの

1. 呼んでいる関数が実在すること(欠けた属性は裸の except に消える)
2. 会話 ID を持つセッションでは driver に**空でない** conversation_id が渡ること
3. 読み取りに失敗したときは黙らないこと — 「聞けなかった」が「聞いたら空だった」に
   見えてはいけない。これがこの欠陥を隠していた当のもの
"""
import io
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _driver_fn_source():
    with io.open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def _bridge_socket_driver("):]
    return body[:body.index("\ndef ", 10)]


def test_session_store_has_no_get_so_nothing_may_call_it():
    """`S.get` を呼んでいる箇所が無いこと。存在しない属性は裸の except に消える。"""
    import bridge.session_store as S

    assert not hasattr(S, "get"), (
        "session_store に get() が生えた -- ならこのテストの前提を書き直すこと")
    assert hasattr(S, "load")

    with io.open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "S.get(" not in src, "存在しない S.get を呼んでいる箇所が残っている"


def test_the_conversation_id_is_read_with_the_function_that_exists():
    body = _driver_fn_source()
    assert "S.load(ACTIVE_SID)" in body, "会話 ID を S.load で読んでいない"
    assert "conversation_id=conv_id" in body, "読んだ ID を driver に渡していない"


def test_a_failed_read_is_reported_not_swallowed():
    """失敗を黙って空 ID にしない。

    続行自体は正しい(読み取りに失敗しただけでブリッジが黙るほうが悪い)。
    しかし黙って続行したことが、この欠陥をここまで隠した当のものだった。
    """
    body = _driver_fn_source()
    m = re.search(r"except Exception as exc:\s*\n\s*conv_id = \"\"\s*\n\s*logger\.warning",
                  body)
    assert m, "会話 ID の読み取り失敗が記録されていない(裸の except のままになっている)"
    assert "START A NEW conversation" in body, (
        "失敗の帰結(新しい会話が始まる)が記録に書かれていない")


def test_both_stored_conversation_shapes_yield_an_id():
    """ページ由来の conv_url と、ソケット由来の sess:<guid> の**両方**から ID が出ること。

    ソースの形ではなく値で見る。S.load に直すだけでは足りなかった: ソケットが書く形は
    "sess:" + guid で、_conv_guid はそれに対して "" を返す。つまり**いま一番多い経路**で
    会話 ID は空のままだった。このテストはそれを見つけて落ちた。
    """
    from bridge.copilot_bridge import _conv_guid, make_sessref, sessref_guid

    guid = "d870f6cd-4aa5-4d42-9626-ab690c041a1a"
    page_url = "https://m365.cloud.microsoft/chat/conversation/%s?titleId=T_x" % guid
    socket_ref = make_sessref(guid)

    pick = lambda ref: sessref_guid(ref) or _conv_guid(ref) or ""  # noqa: E731
    assert pick(page_url) == guid, "ページ由来の conv_url から ID が出ない"
    assert pick(socket_ref) == guid, "ソケット由来の sess:<guid> から ID が出ない"
    assert pick("") == ""


def test_the_driver_read_uses_both_shapes():
    """実装が両方の形を見ていること。片方だけなら、その経路は静かに新規会話になる。"""
    body = _driver_fn_source()
    assert "sessref_guid(" in body, "ソケット由来の形を読んでいない"
    assert "_conv_guid(" in body, "ページ由来の形を読んでいない"