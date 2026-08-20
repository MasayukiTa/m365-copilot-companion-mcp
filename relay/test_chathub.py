"""ソケット経路が、**認証には決して触れない**ことを構造として固定する。

この経路を最初に切り拓いた公開ツールは、Microsoft 自身のファーストパーティ・クライアントID
を名乗り、それらが共有する family refresh token の性質を使ってトークンを取る。2022年から
悪用手法として文書化され、検知側のツールまである形で、テナントのアプリ同意ガバナンスを
迂回する。**それはやらない。**

やるのは「サインイン済みのブラウザが、同じ利用者のために、同じバックエンド向けに既に
持っているトークンを使う」ことだけ。きれいではない（相手は非公開 API で無告知に壊れる）が、
「発行済みのものを使う」と「発行されていないものを作る」の差がこの区別の全てである。

だから境界はコメントではなくテストに置く。
"""
import json
import time
import uuid

import pytest

from relay import chathub as CH


# ---- 認証に触れないこと（ここが本体） ---------------------------------------------------------

def test_no_identity_provider_appears_anywhere_in_this_module():
    """トークンを名乗れる場所はトークンエンドポイントしかない。そこへ行かなければ、
    クライアントIDを詐称する余地が構造的に無い。"""
    src = open(CH.__file__, encoding="utf-8").read()
    body = src.split('"""', 2)[-1]          # モジュール docstring は説明のため言及してよい
    for host in ("login.microsoftonline.com", "login.microsoft.com",
                 "oauth2/v2.0/token", "oauth2/v2.0/authorize", "devicecode",
                 "client_secret", "client_id", "refresh_token"):
        assert host not in body, (
            "%r がコード中に現れた。この経路は認証を行わない -- ブラウザが署名し、"
            "ここは喋るだけ" % host)


def test_a_bare_token_string_is_refused_so_nobody_hardcodes_one():
    with pytest.raises(CH.ChatHubError) as exc:
        CH.Conversation("eyJ0eXAiOiJKV1QifQ.e30.x")
    assert "callable" in str(exc.value)


def test_the_supplier_is_asked_every_turn_not_cached_forever(monkeypatch):
    """トークンは1時間で切れる。更新もブラウザにさせるので、毎回聞き直す必要がある。"""
    calls = []

    def supplier():
        calls.append(1)
        return _token(exp_in=3600)

    conv = CH.Conversation(supplier)
    conv.url_for_turn(str(uuid.uuid4()))
    conv.url_for_turn(str(uuid.uuid4()))
    assert len(calls) == 2, "トークンを抱え込んでいる"


def test_an_expired_token_is_refused_with_a_pointer_to_the_browser():
    conv = CH.Conversation(lambda: _token(exp_in=-10))
    with pytest.raises(CH.ChatHubError) as exc:
        conv.url_for_turn("r")
    assert "browser" in str(exc.value) and "renew" in str(exc.value)


def test_an_unreadable_token_counts_as_expired_rather_than_as_valid():
    """読めないトークンを有効扱いすると、失敗が確定した接続を張りに行く。"""
    assert CH.expires_in("not-a-jwt") == 0.0


# ---- Origin は既定で送らない（測定を先送りしないため） ---------------------------------------

def test_origin_is_not_sent_unless_the_caller_asks():
    """サーバが Origin を要求するかは未測定。既定で送ると、その問いに
    『一度も尋ねないこと』で答えてしまう。"""
    assert CH.Conversation(lambda: _token()).headers() == {}


def test_origin_can_be_opted_into_explicitly():
    h = CH.Conversation(lambda: _token(), send_origin=True).headers()
    assert h["Origin"] == CH.BROWSER_ORIGIN


def test_no_user_agent_is_invented_by_default():
    """参照実装は Firefox の UA を詐称する。既定では何も名乗らない。"""
    assert "User-Agent" not in CH.Conversation(lambda: _token()).headers()


# ---- URL 組み立て -------------------------------------------------------------------------

def test_the_url_is_keyed_by_the_token_s_own_oid_and_tid():
    """oid/tid はトークンの中にある。別途どこかから取ってくる必要はない
    -- つまりトークン1つでこの経路は完結する。"""
    tok = _token(oid="OID-1", tid="TID-2")
    url = CH.build_ws_url(tok, session_id="s", conversation_id="c", request_id="r")
    assert url.startswith(CH.WS_BASE + "/OID-1@TID-2?")
    assert "access_token=" in url


def test_a_token_without_oid_or_tid_is_refused():
    with pytest.raises(CH.ChatHubError) as exc:
        CH.build_ws_url(_token(oid=None), session_id="s", conversation_id="c", request_id="r")
    assert "oid/tid" in str(exc.value)


# ---- フレーム ----------------------------------------------------------------------------

def test_a_turn_sends_the_chat_frame_and_the_metrics_frame_together():
    """プロトコルが同一送信を要求する。分けると応答が来ない。"""
    blob = CH.chat_frames("hello", session_id="s", conversation_id="c", request_id="r")
    frames = CH.parse_frames(blob)
    assert [f["type"] for f in frames] == [4, 1]
    assert frames[0]["target"] == "chat"


def test_frames_are_terminated_by_the_record_separator_not_by_newlines():
    blob = CH.chat_frames("hi", session_id="s", conversation_id="c", request_id="r")
    assert blob.endswith(CH.RS)
    assert blob.count(CH.RS) == 2


def test_the_text_survives_unicode_intact():
    blob = CH.chat_frames("請求書の合計を出して", session_id="s", conversation_id="c",
                          request_id="r")
    sent = CH.parse_frames(blob)[0]["arguments"][0]["message"]["text"]
    assert sent == "請求書の合計を出して"


def test_the_timezone_is_this_machine_s_not_a_constant_from_someone_else_s_probe():
    """参照実装は Asia/Shanghai を固定で送る。位置情報はモデルに届き、
    『明日』『今朝』の解釈に使われるので、居ない場所を名乗る意味がない。"""
    info = CH.parse_frames(
        CH.chat_frames("x", session_id="s", conversation_id="c", request_id="r")
    )[0]["arguments"][0]["message"]["locationInfo"]
    assert info["timeZone"] == (time.tzname[0] or "UTC")
    assert isinstance(info["timeZoneOffset"], int)


def test_a_frame_that_will_not_parse_does_not_lose_the_rest():
    """ストリームには keep-alive やモデル化していない形が混じる。1つで止まると
    見た目の驚きが失われたターンに化ける。"""
    blob = '{"type":6}' + CH.RS + "not json at all" + CH.RS + '{"type":3}' + CH.RS
    assert [f["type"] for f in CH.parse_frames(blob)] == [6, 3]


def test_text_is_only_taken_from_update_frames():
    assert CH.collect_text({"type": 1, "target": "update",
                            "arguments": [{"writeAtCursor": "abc"}]}) == "abc"
    assert CH.collect_text({"type": 1, "target": "Metrics",
                            "arguments": [{"writeAtCursor": "nope"}]}) == ""


def test_completion_and_ping_are_told_apart():
    assert CH.is_complete({"type": 3}) and not CH.is_ping({"type": 3})
    assert CH.is_ping({"type": 6}) and not CH.is_complete({"type": 6})


# ---- 補助 --------------------------------------------------------------------------------

def _token(oid="oid-x", tid="tid-y", exp_in=3600):
    import base64
    payload = {"exp": int(time.time()) + exp_in}
    if oid:
        payload["oid"] = oid
    if tid:
        payload["tid"] = tid
    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return "eyJ0eXAiOiJKV1QifQ." + raw + ".sig"
