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


# ---- ターンのループ（ソケットは注入なので、ネットワーク無しで確かめられる） ------------------------

class _FakeSock:
    """A scripted socket. The seam exists so the protocol can be tested without a network."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, blob):
        self.sent.append(blob)

    def recv(self, _timeout):
        return self.script.pop(0) if self.script else None

    def close(self):
        pass


def _connect_returning(*scripts):
    socks = [_FakeSock(s) for s in scripts]
    made = []

    def _connect(_url, _headers, _timeout):
        made.append(socks[len(made)])
        return made[-1]

    _connect.made = made
    return _connect


def _update(text):
    return json.dumps({"type": 1, "target": "update",
                       "arguments": [{"writeAtCursor": text}]}) + CH.RS


def _done():
    return json.dumps({"type": 3}) + CH.RS


def test_a_turn_returns_the_text_between_handshake_and_completion():
    connect = _connect_returning(["{}" + CH.RS, _update("答え"), _done()])
    conv = CH.Conversation(lambda: _token())
    assert conv.ask("質問", connect=connect) == "答え"
    assert conv.turns == 1


def test_the_handshake_goes_first_and_the_chat_frame_second():
    connect = _connect_returning(["{}" + CH.RS, _done()])
    CH.Conversation(lambda: _token()).ask("x", connect=connect)
    sent = connect.made[0].sent
    assert sent[0] == '{"protocol":"json","version":1}' + CH.RS
    assert '"target": "chat"' in sent[1] or '"target":"chat"' in sent[1]


def test_a_ping_is_answered_so_the_far_end_keeps_the_socket():
    connect = _connect_returning(["{}" + CH.RS,
                                  json.dumps({"type": 6}) + CH.RS,
                                  _update("ok"), _done()])
    assert CH.Conversation(lambda: _token()).ask("x", connect=connect) == "ok"
    assert json.dumps({"type": 6}) + CH.RS in connect.made[0].sent


def test_a_socket_that_goes_silent_is_not_a_finished_turn():
    """type-3 は『ターンが完了した』であって『接続が生きている』ではない。
    無音を完了と読むと、死んだソケットが空の答えとして通る。"""
    connect = _connect_returning(["{}" + CH.RS, _update("途中まで")])   # then None
    with pytest.raises(CH.ChatHubError) as exc:
        CH.Conversation(lambda: _token()).ask("x", connect=connect)
    assert "silent" in str(exc.value)


def test_the_frame_budget_is_bounded():
    flood = ["{}" + CH.RS] + [_update("x")] * 50
    connect = _connect_returning(flood)
    conv = CH.Conversation(lambda: _token(), max_frames=10)
    with pytest.raises(CH.ChatHubError) as exc:
        conv.ask("x", connect=connect)
    assert "frame budget" in str(exc.value)


# ---- ツールループは send の内側に隠れること（判断機に生ブロックを見せない） ------------------------

def test_the_tool_loop_runs_inside_and_the_caller_sees_only_the_answer():
    """上流の判断機（stuck 検出・拒否検出・settle）は『答え』を読む。
    生の fenced block を見せると、返信ではなく要求について推論し始める。"""
    call = '```call_tool\n{"name": "ls"}\n```'
    connect = _connect_returning(
        ["{}" + CH.RS, _update("確認します。" + chr(10) + call), _done()],  # 1回目: ツール要求
        ["{}" + CH.RS, _update("3件ありました。"), _done()],        # 2回目: 最終回答
    )
    ran = []

    def run_tool(name, args):
        ran.append((name, args))
        return True, "a b c"

    conv = CH.Conversation(lambda: _token())
    out = conv.ask("数えて", connect=connect, run_tool=run_tool,
                   catalogue=[{"name": "call_tool", "description": "gateway"}])
    assert ran == [("call_tool", {"name": "ls"})]
    assert out == "3件ありました。"
    assert "```" not in out, "生の fenced block が呼び出し側へ漏れている"


def test_the_tool_result_is_what_the_second_turn_carries():
    connect = _connect_returning(
        ["{}" + CH.RS, _update('```call_tool\n{"name": "ls"}\n```'), _done()],
        ["{}" + CH.RS, _update("done"), _done()],
    )
    CH.Conversation(lambda: _token()).ask(
        "x", connect=connect, run_tool=lambda n, a: (True, "RESULT-MARKER"),
        catalogue=[{"name": "call_tool", "description": "d"}])
    assert "RESULT-MARKER" in connect.made[1].sent[1]


def test_a_model_that_never_stops_calling_is_bounded():
    """呼び続けて結論しないモデルは進捗していない。無限ループはゴール1本の予算を
    1ターンで使い切る。"""
    call = ["{}" + CH.RS, _update('```call_tool\n{"name": "again"}\n```'), _done()]
    connect = _connect_returning(*([call] * 10))
    conv = CH.Conversation(lambda: _token(), max_tool_rounds=3)
    with pytest.raises(CH.ChatHubError) as exc:
        conv.ask("x", connect=connect, run_tool=lambda n, a: (True, "r"),
                 catalogue=[{"name": "call_tool", "description": "d"}])
    assert "tool rounds exceeded" in str(exc.value)


def test_only_the_first_exchange_of_a_conversation_says_it_is_the_start():
    connect = _connect_returning(["{}" + CH.RS, _done()], ["{}" + CH.RS, _done()])
    conv = CH.Conversation(lambda: _token())
    conv.ask("one", connect=connect)
    conv.ask("two", connect=connect)
    first = json.loads(connect.made[0].sent[1].split(CH.RS)[0])
    second = json.loads(connect.made[1].sent[1].split(CH.RS)[0])
    assert first["arguments"][0]["isStartOfSession"] is True
    assert second["arguments"][0]["isStartOfSession"] is False


def test_the_socket_is_closed_even_when_the_turn_fails():
    closed = []

    class Sock(_FakeSock):
        def close(self):
            closed.append(1)

    def connect(_u, _h, _t):
        return Sock(["{}" + CH.RS])          # no completion -> raises

    with pytest.raises(CH.ChatHubError):
        CH.Conversation(lambda: _token()).ask("x", connect=connect)
    assert closed == [1]


# ---- 実測から起こしたテスト (2026-08-20) --------------------------------------------------
#
# ここから下は全て「動かして分かったこと」を固定するもの。推測で書いた形は4回外し、
# 実クライアントを捕獲して差分を取った瞬間に全部解けた。同じ間違いを繰り返さないための錠。

def _urls_recording_connect(*scripts):
    """接続ごとに URL を控える connect。鍵が接続に属することを検証するために必要。"""
    socks = [_FakeSock(s) for s in scripts]
    made, urls = [], []

    def _connect(url, _headers, _timeout):
        urls.append(url)
        made.append(socks[len(made)])
        return made[-1]

    _connect.made, _connect.urls = made, urls
    return _connect


def _q(url):
    from urllib.parse import urlsplit, parse_qsl
    return dict(parse_qsl(urlsplit(url).query))


def test_one_key_names_the_connection_and_a_new_connection_gets_a_new_one():
    """実測: クライアントは1メッセージごとに socket を開き直し、そのたびに
    chatsessionid / clientrequestid / XRoutingParameterSessionKey に同一の新しい値を入れる。
    こちらは鍵を会話単位で使い回しており、2ターン目以降が全て InvalidRequest だった。"""
    connect = _urls_recording_connect(["{}" + CH.RS, _update("a"), _done()],
                                      ["{}" + CH.RS, _update("b"), _done()])
    conv = CH.Conversation(lambda: _token())
    conv.ask("one", connect=connect)
    conv.ask("two", connect=connect)

    first, second = _q(connect.urls[0]), _q(connect.urls[1])
    for q in (first, second):
        assert q["chatsessionid"] == q["clientrequestid"] == q["XRoutingParameterSessionKey"]
    assert first["chatsessionid"] != second["chatsessionid"]
    # 会話 id は据え置き。継続性は接続ではなく会話 id が担う。
    assert first["ConversationId"] == second["ConversationId"]


def test_a_snapshot_is_not_added_to_the_deltas_that_built_it():
    """実測: 同じ答えが delta・type1 の messages・type2 の item と3経路で届き、
    素朴に連結すると 166 が "166166166" になった。"""
    snapshot = json.dumps({"type": 2, "item": {"messages": [
        {"author": "bot", "messageType": "Chat", "text": "166"}]}}) + CH.RS
    connect = _urls_recording_connect(["{}" + CH.RS, _update("1"), _update("66"),
                                       snapshot, _done()])
    assert CH.Conversation(lambda: _token()).ask("x", connect=connect) == "166"


def test_deltas_are_used_when_no_snapshot_arrives():
    connect = _urls_recording_connect(["{}" + CH.RS, _update("16"), _update("6"), _done()])
    assert CH.Conversation(lambda: _token()).ask("x", connect=connect) == "166"


def test_a_declined_request_carries_the_backend_s_own_reason():
    """実測: 拒否は無言ではなく item.result.value に理由が入る。読まなかったので
    『空文字を返す寡黙なモデル』に見え、fallback しても記録に残る理由が無かった。"""
    declined = json.dumps({"type": 2, "item": {"result": {"value": "InvalidRequest"}}}) + CH.RS
    connect = _urls_recording_connect(["{}" + CH.RS, declined, _done()])
    with pytest.raises(CH.ChatHubError) as exc:
        CH.Conversation(lambda: _token()).ask("x", connect=connect)
    assert "InvalidRequest" in str(exc.value)


def test_a_template_supplies_the_shape_and_the_turn_supplies_only_its_ids():
    """捕獲した形はそのまま、ターン固有の値だけが上書きされる。
    conversationId はフレームに入れない -- クライアントは URL にしか入れていない。"""
    captured = {
        "variants_marker": "kept",
        "optionsSets": ["a", "b"],
        "conversationId": "someone-elses",
        "threadLevelGptId": {"id": "T_agent.x", "source": "MOS3"},
        "isStartOfSession": False,
        "clientInfo": {"clientSessionId": "old"},
        "message": {"text": "old", "requestId": "old", "entityAnnotationTypes": ["People"]},
    }
    tpl = CH.RequestTemplate({"agent": "Agent", "gptId": "T_agent.x@MOS3"}, captured)
    args = json.loads(CH.chat_frames("新しい質問", session_id="S", conversation_id="C",
                                     request_id="R", started=True,
                                     template=tpl).split(CH.RS)[0])["arguments"][0]

    assert args["variants_marker"] == "kept" and args["optionsSets"] == ["a", "b"]
    assert "conversationId" not in args
    assert args["clientInfo"]["clientSessionId"] == "S"
    assert args["message"]["text"] == "新しい質問" and args["message"]["requestId"] == "R"
    assert args["message"]["entityAnnotationTypes"] == ["People"]   # 捕獲物は削らない
    assert args["traceId"] == args["clientCorrelationId"] == "R"


def test_a_template_never_carries_a_token():
    """テンプレートは持ち回され、ログにも載る。資格情報が同居してはならない。"""
    tpl = CH.RequestTemplate({"access_token": "SECRET", "agent": "Agent"}, {})
    assert "access_token" not in tpl.query
    assert "SECRET" not in json.dumps(tpl.query)


def test_the_template_s_query_reaches_the_url_and_the_composed_default_does_not():
    tpl = CH.RequestTemplate({"agent": "Agent", "scenario": "officeweb",
                              "gptId": "T_agent.x@MOS3"}, {})
    url = CH.build_ws_url(_token(), session_id="s", conversation_id="c", request_id="r",
                          template=tpl)
    q = _q(url)
    assert q["agent"] == "Agent" and q["scenario"] == "officeweb"
    assert q["gptId"] == "T_agent.x@MOS3"


def test_a_conversation_takes_its_agent_from_its_template():
    """エージェントを指定し忘れた socket は既定 Copilot に届く -- ツールもテナント接地も無い
    別物が答える。テンプレートがエージェントを知っているなら、それを既定にする。"""
    tpl = CH.RequestTemplate({}, {"threadLevelGptId": {"id": "T_agent.x", "source": "MOS3"}})
    assert CH.Conversation(lambda: _token(), template=tpl).gpt_id == "T_agent.x"


# ---- 進捗（実測 2026-08-21） -----------------------------------------------------------------
#
# Researcher は調査中に messageType="Progress" / contentOrigin="ChainOfThoughtSummary" を流す。
# 実機で捕獲した1件は 5,332 文字あり、参照したベンチマーク名と採った数値が並んでいた。
# つまり「どの経路で調べ、何を根拠に判断したか」は最初から線の上にあり、こちらが捨てていた。

def _progress_frame(text, mtype="Progress", origin="ChainOfThoughtSummary", author="bot"):
    return {"type": 2, "item": {"messages": [
        {"messageType": mtype, "contentOrigin": origin, "author": author, "text": text}]}}


def test_progress_is_kept_and_not_mistaken_for_the_answer():
    frame = _progress_frame("BEIR では BM25 が強いベースラインのままで…")
    prog = CH.collect_progress(frame)
    assert len(prog) == 1
    assert prog[0]["type"] == "Progress"
    assert prog[0]["origin"] == "ChainOfThoughtSummary"
    assert "BEIR" in prog[0]["text"]
    # 回答ではない。思考は「モデルが言ったこと」ではない。
    assert CH.collect_final(frame) == ""
    assert CH.collect_text(frame) == ""


def test_the_answer_is_not_collected_as_progress():
    frame = {"type": 2, "item": {"messages": [
        {"messageType": "Chat", "author": "bot", "text": "166"}]}}
    assert CH.collect_progress(frame) == []
    assert CH.collect_final(frame) == "166"


def test_a_turn_exposes_the_progress_it_saw():
    connect = _urls_recording_connect(["{}" + CH.RS,
                                       json.dumps(_progress_frame("検索クエリを組み立てています")) + CH.RS,
                                       _update("できました"), _done()])
    conv = CH.Conversation(lambda: _token())
    assert conv.ask("x", connect=connect) == "できました"
    assert [p["text"] for p in conv.last_progress] == ["検索クエリを組み立てています"]


def test_progress_from_a_previous_turn_does_not_leak_into_the_next():
    """前のターンの思考を今のターンの根拠として渡すと、検証が別物を検証する。"""
    first = ["{}" + CH.RS, json.dumps(_progress_frame("一度目")) + CH.RS, _update("A"), _done()]
    second = ["{}" + CH.RS, _update("B"), _done()]
    connect = _urls_recording_connect(first, second)
    conv = CH.Conversation(lambda: _token())
    conv.ask("one", connect=connect)
    assert len(conv.last_progress) == 1
    conv.ask("two", connect=connect)
    assert conv.last_progress == []


def test_progress_can_be_watched_while_it_arrives():
    seen = []
    connect = _urls_recording_connect(["{}" + CH.RS,
                                       json.dumps(_progress_frame("一歩目")) + CH.RS,
                                       json.dumps(_progress_frame("二歩目")) + CH.RS,
                                       _update("完了"), _done()])
    CH.Conversation(lambda: _token()).ask("x", connect=connect,
                                          on_progress=lambda p: seen.append(p["text"]))
    assert seen == ["一歩目", "二歩目"]


def test_a_progress_callback_that_raises_does_not_cost_the_turn():
    connect = _urls_recording_connect(["{}" + CH.RS,
                                       json.dumps(_progress_frame("一歩目")) + CH.RS,
                                       _update("完了"), _done()])

    def boom(_p):
        raise RuntimeError("表示側の都合")

    assert CH.Conversation(lambda: _token()).ask("x", connect=connect,
                                                 on_progress=boom) == "完了"


def test_progress_from_the_user_is_not_collected():
    """author が bot でないものは、こちらが送った文の反射でしかない。"""
    frame = _progress_frame("これは自分が送った文", author="user")
    assert CH.collect_progress(frame) == []
