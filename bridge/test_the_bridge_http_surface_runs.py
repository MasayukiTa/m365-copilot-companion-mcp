# -*- coding: utf-8 -*-
"""bridge の HTTP 面を**実際に立てて**叩く。ソースを読むのではなく。

## なぜ今まで誰も実行していなかったか

bridge のテストは全部ソース断言で、理由が各ファイルに書いてある:

    "copilot_bridge.py imports Playwright at module scope and cannot be imported on a runner."

**実測 2026-09-22: import は 2.6 秒で通り、スレッドは1本、サーバも立たない。**
Playwright の import は 6944 行目、関数の中にある。module scope には無い。
いつかの時点で直り、**この一文だけが残って、スイート全体から実行する能力を奪っていた。**

## 何を立てるか — 稼働中の bridge ではない

**空きポートに `Handler` を自分で立てる。** 運用中の bridge (`MCP_BRIDGE_URL`、既定 :8765) は
叩かない。ページを掴むエンドポイント (`/stream` `/goal` `/new` `/switch` `/history`
`/upload`) は**一度叩いてEdgeの自傷ループを着火した前科がある**ので、ここでも触らない。

使うのは `/send` だけ。このエンドポイントは設計上 store-only で、自分のコメントが
「never touches PAGE」と宣言している — **その宣言を、実行して確かめる。**

## セッションストアは必ず隔離する

`MCP_SESSION_STORE_DIR`。名前を当て推量して `BRIDGE_SESSION_DIR` を設定した結果、
**オペレータの実ストアに行が1つ増えた**（消した）。`session_store.py` の同じ定数には
「テスト実行が138行を実ストアに書いた」という記録が既にある。2回目だった。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def bridge_http(tmp_path, monkeypatch):
    """A real HTTP server running the real Handler, on a port nobody else has."""
    # BEFORE THE IMPORT. session_store reads this at call time, but a module that has already
    # decided its directory in a previous test would keep it, and the cost of being wrong here
    # is a write into the operator's live store.
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import session_store as S

    monkeypatch.setattr(S, "SESS_DIR", str(tmp_path / "sessions"), raising=False)
    assert S._base_dir() == str(tmp_path / "sessions"), \
        "the store is not isolated; refusing to run rather than write into the real one"

    import bridge.copilot_bridge as B
    from http.server import HTTPServer

    srv = HTTPServer(("127.0.0.1", 0), B.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1], B
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _get(base, path, timeout=15):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _send(base, msg, sid=None):
    """Exactly the shape the chat window builds: percent-encoded query values.

    ui/CopilotChat.cs uses Uri.EscapeDataString, which encodes a space as %20 and never as
    '+'. urllib.parse.quote with no safe characters is the same contract, which is what makes
    this a test of the two sides agreeing rather than of one side alone.
    """
    q = "/send?msg=" + urllib.parse.quote(msg, safe="")
    if sid:
        q += "&sid=" + urllib.parse.quote(sid, safe="")
    return _get(base, q)


def test_the_endpoint_answers_at_all(bridge_http):
    """**この1行が今日まで存在しなかった。** bridge の HTTP 面を実行した検査はゼロ。"""
    base, _ = bridge_http
    assert _send(base, "tidy the docs")["ok"] is True


def test_an_empty_message_is_refused(bridge_http):
    base, _ = bridge_http
    r = _send(base, "   ")
    assert r["ok"] is False and "empty" in r["error"]


@pytest.mark.parametrize("msg", [
    "plain words",
    "has spaces and  doubles",
    "ampersand & equals = percent % plus +",
    "日本語と絵文字 🚀 と \"quotes\" と 'single'",
    "newline\nand\ttab",
    "url-ish: http://example.com/a?b=c&d=e#f",
    "a" * 2000,
])
def test_the_message_arrives_exactly_as_it_was_sent(bridge_http, msg):
    """**これが契約の本体。** C# が組んだ URL を Python が解いて、同じ文字列が出る。

    ここが壊れる典型は `&` と `+` と空白: `parse_qs` は `+` を空白に復号するので、
    エスケープしない側がいると本文が静かに変わる。読んで分かることではない。
    """
    base, B = bridge_http
    from bridge import session_store as S

    r = _send(base, msg)
    assert r["ok"] is True
    pending = (S.load(r["sid"]) or {}).get("pending") or []
    assert pending and pending[-1] == msg, \
        "the message changed between the window and the store"


def test_it_really_does_not_touch_the_page(bridge_http):
    """`/send` の自己申告 "never touches PAGE" を**実行して**確かめる。

    このサーバにはページ所有スレッドが無い。ページに触れる実装なら、ここで例外か
    ハングになる。返ってくること自体が、触っていないことの証拠になる。
    """
    base, B = bridge_http
    assert B.PAGE is None, "the fixture is not as hermetic as this test assumes"
    assert _send(base, "no page here")["ok"] is True
    assert B.PAGE is None, "/send created or touched a page"


def test_the_reply_does_not_claim_a_turn_it_cannot_know_started(bridge_http):
    """**フィールドは直され、散文は同じ主張を続けていた。**

    `promoted` は「この返答には分からない結末を主張していた」として
    `promotion_attempted` に改名済み。ところが note は
    "queued, and a turn is being run for it now." と言い続けていた。
    ページ所有スレッドが無いここでは `_promote` が即座に失敗し、メッセージは
    キューに残る — それでも「いま走っている」と返っていた。
    """
    base, _ = bridge_http
    r = _send(base, "is a turn really running?")
    assert r["promotion_attempted"] is True
    assert "is being run for it now" not in r["note"], \
        "the reply asserts a turn started, which it cannot know and here is not true"
    assert "stays queued" in r["note"]


def test_the_queue_depth_is_the_store_and_not_a_guess(bridge_http):
    base, _ = bridge_http
    first = _send(base, "one")
    sid = first["sid"]
    second = _send(base, "two", sid=sid)
    assert second["sid"] == sid
    assert second["queue_depth"] == 2, second
