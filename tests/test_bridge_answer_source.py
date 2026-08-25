"""ブリッジが答えをどこから読むか。常駐タブを手放すための最後の結び目。

ブリッジのターンループは DRIVER に答えを訊いていなかった -- ページを直接掻いていた
（clean な markdown 本文、伸びている `loading-message`、完了時に埋まる `lastChatMessage`）。
だから「ソケットに替える」は1箇所の置換ではなく、ソケット側に対応物のない4つの読みだった。

ここで見張るのは2つ:
  * ページ経路の挙動が**1バイトも変わっていない**こと。この種の改修が壊すのは、
    誰も切り替えていない側のフォールバック順序である。
  * ソケット経路が、ページと同じ意味を返すこと。とくに settled が走行中に空であること --
    ここが埋まると、ループは末尾を切り落として完了扱いにする。
"""
import pytest

from bridge import copilot_bridge as B


class _SocketDrv:
    """本物と同じ契約。以前このスタブは欠陥の側を写していた --
    `read_last_response` が前ターンの本文に落ちる形で、それをアクセサが使っていたので、
    テストは緑のままバグを固定していた。今は「そのターンが答えたか」を持つ。"""

    IS_SOCKET = True

    def __init__(self, partial="", last="", generating=False, answered=None):
        self._partial, self._last, self._gen = partial, last, generating
        self._answered = bool(last) if answered is None else answered
        self.failed = ""

    def partial_text(self):
        return self._partial

    def settled_text(self):
        if self._gen or not self._answered:
            return ""
        return self._last

    def read_last_response(self):
        return self._partial or (self._last if self._answered else "")

    def conversation_ids(self):
        return {"server": getattr(self, "conv_id", ""), "client": "", "session": "", "turns": 1}

    def close(self):
        self.closed = True


class _PageDrv:
    """タブ用ドライバは IS_SOCKET を名乗らない。"""


@pytest.fixture
def dom(monkeypatch):
    """ページ経路。clean 本文と2つのセレクタを差し替えて、読まれた順序を記録する。"""
    state = {"clean": None, "loading": "", "lastmsg": "", "reads": []}

    def _clean():
        state["reads"].append("clean")
        return state["clean"]

    def _text(sel):
        state["reads"].append(sel)
        return state["loading"] if sel == B.LOADING else state["lastmsg"]

    monkeypatch.setattr(B, "DRIVER", _PageDrv())
    monkeypatch.setattr(B, "_clean_answer_text", _clean)
    monkeypatch.setattr(B, "_text", _text)
    return state


# ---- ページ経路: 以前の式そのままであること --------------------------------------------------

def test_partial_prefers_the_clean_body_over_the_loading_selector(dom):
    dom["clean"], dom["loading"] = "本文", "処理中"
    assert B._answer_partial() == "本文"


def test_partial_falls_back_to_loading_when_the_clean_body_is_empty(dom):
    dom["clean"], dom["loading"] = None, "処理中"
    assert B._answer_partial() == "処理中"
    assert dom["reads"] == ["clean", B.LOADING]


def test_settled_falls_back_to_lastmsg_not_to_loading(dom):
    dom["clean"], dom["lastmsg"], dom["loading"] = None, "完了本文", "処理中"
    assert B._answer_settled() == "完了本文"
    assert B.LOADING not in dom["reads"], "settled が LOADING を読んでいる"


def test_clean_has_no_fallback_at_all(dom):
    """ループ内側の読み。ここで LASTMSG に落ちるのは既知の不具合だった
    -- 生テキストは見出しとalt文言を含み、clean 本文と決して一致せず、
    整定窓が毎回リセットされて600秒回り続けた。"""
    dom["clean"], dom["lastmsg"] = None, "生テキスト"
    assert B._answer_clean() == ""
    assert dom["reads"] == ["clean"]


def test_a_page_driver_is_not_mistaken_for_a_socket(dom):
    assert B._on_socket() is False


# ---- ソケット経路: 同じ意味を返すこと ----------------------------------------------------------

def test_a_running_turn_never_yields_a_settled_answer(monkeypatch):
    """埋まった瞬間にループは整定に入る。走行中に返すと末尾を落とす。"""
    monkeypatch.setattr(B, "DRIVER", _SocketDrv(partial="途中まで", generating=True))
    assert B._answer_settled() == ""
    assert B._answer_partial() == "途中まで"
    assert B._on_socket() is True


def test_a_finished_turn_yields_its_answer(monkeypatch):
    monkeypatch.setattr(B, "DRIVER", _SocketDrv(last="最終形", generating=False))
    assert B._answer_settled() == "最終形"
    assert B._answer_clean() == "最終形"


def test_the_socket_path_never_touches_the_page(monkeypatch):
    """常駐タブを外すのが目的なので、ソケット中に DOM を読んだら本末転倒。"""
    def boom(*a, **kw):
        raise AssertionError("socket 経路がページを読んでいる")

    monkeypatch.setattr(B, "DRIVER", _SocketDrv(partial="p", last="f"))
    monkeypatch.setattr(B, "_clean_answer_text", boom)
    monkeypatch.setattr(B, "_text", boom)
    B._answer_partial()
    B._answer_settled()
    B._answer_clean()


def test_the_real_socket_driver_satisfies_all_three_reads():
    """スタブだけで通しても意味がない。本物のクラスが3つとも持っていること。"""
    from relay.socket_driver import CopilotSocketDriver as D

    assert D.IS_SOCKET is True
    for name in ("partial_text", "settled_text", "read_last_response", "wait_for_idle"):
        assert callable(getattr(D, name, None)), name


def test_the_turn_loop_reads_only_through_these_three(monkeypatch):
    """アクセサを足しても、呼び出し側が古い読みを1箇所でも残していれば
    その経路だけページに縛られたままになる。"""
    import inspect

    src = inspect.getsource(B.Handler._send_and_stream_once)
    assert "_clean_answer_text()" not in src, "生の DOM 読みが残っている"
    assert "_text(LOADING)" not in src and "_text(LASTMSG)" not in src
    for name in ("_answer_partial()", "_answer_settled()", "_answer_clean()"):
        assert name in src, name


# ---- 経路の選択: 会話はソケット、ページは DOM 専用 -----------------------------------------

def test_a_live_socket_is_reused_rather_than_rebuilt(monkeypatch):
    built = []
    monkeypatch.setattr(B, "DRIVER", _SocketDrv(last="x"))
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: built.append(1) or _SocketDrv())
    assert B.ensure_driver() is not None
    assert built == [], "生きているソケットを毎ターン作り直している"


def test_a_failed_socket_is_replaced_not_kept(monkeypatch):
    dead = _SocketDrv()
    dead.failed = "ConnectionClosedError"
    fresh = _SocketDrv(last="y")
    monkeypatch.setattr(B, "DRIVER", dead)
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: fresh)
    assert B.ensure_driver() is fresh


def test_without_a_socket_it_falls_back_to_the_page(monkeypatch):
    page = _PageDrv()
    monkeypatch.setattr(B, "DRIVER", None)
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: None)

    def _ensure():
        B.DRIVER = page
        return True

    monkeypatch.setattr(B, "ensure_page_alive", _ensure)
    monkeypatch.setattr(B, "run_on_page_thread", lambda fn, *a, **kw: fn(*a, **kw))
    assert B.ensure_driver() is page


def test_reopening_the_page_does_not_steal_a_socket_conversation(monkeypatch):
    """4つの DOM 用エンドポイントがページを必要としただけで、
    走っている会話が黙って DOM に落ちてはいけない。"""
    sock = _SocketDrv(last="生きている")
    monkeypatch.setattr(B, "DRIVER", sock)
    monkeypatch.setattr(B, "PAGE", None)
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B, "_find_or_open_agent", lambda ctx: object())
    monkeypatch.setattr(B, "CopilotWebDriver",
                        lambda page: pytest.fail("ソケット会話を DOM ドライバで上書きした"))
    assert B.ensure_page_alive() is True
    assert B.DRIVER is sock


def test_the_send_path_is_what_chooses_the_transport():
    """起動時に選ぶと、経路がその一瞬にできたことにプロセス全体が縛られる。"""
    import inspect

    src = inspect.getsource(B._send_counted)
    assert "ensure_driver()" in src


def test_the_switch_is_on_by_default_and_can_be_turned_off():
    import inspect

    src = inspect.getsource(B)
    i = src.index("BRIDGE_SOCKET = ")
    assert 'os.environ.get("MCP_BRIDGE_SOCKET", "1")' in src[i:i + 200]


# ---- ページ所有スレッドからの再投入は自分待ちになる -------------------------------------------

def test_run_on_page_thread_calls_straight_through_on_that_thread(monkeypatch):
    """submit() は実行されるまで呼び出し側を止める。実行役自身が呼べば自分を待つ。
    実際にブリッジを動かした一発目がこれで、全 /stream が
    {"ok": false, "error": "busy"} を返した -- 何が起きたかを一言も言わない理由で。"""
    import threading

    class _Exec:
        def __init__(self):
            self._thread = threading.current_thread()

        def submit(self, fn, *a, **kw):
            raise AssertionError("ページスレッドから再投入している（自分待ち）")

    monkeypatch.setattr(B, "PAGE_EXECUTOR", _Exec())
    assert B.on_page_thread() is True
    assert B.run_on_page_thread(lambda x: x + 1, 41) == 42


def test_another_thread_still_goes_through_the_queue(monkeypatch):
    import threading

    class _Exec:
        def __init__(self):
            self._thread = threading.Thread(target=lambda: None)
            self.calls = []

        def submit(self, fn, *a, **kw):
            self.calls.append(fn)
            return fn(*a, **kw)

    ex = _Exec()
    monkeypatch.setattr(B, "PAGE_EXECUTOR", ex)
    assert B.on_page_thread() is False
    assert B.run_on_page_thread(lambda x: x + 1, 41) == 42
    assert len(ex.calls) == 1, "ページスレッド以外がキューを迂回している"


def test_an_existing_page_driver_does_not_stop_the_socket_being_tried(monkeypatch):
    """起動時にページドライバが作られているので、「ドライバはあるか」だけを見ると
    ソケットは一度も試されない。実際にそう書いてしまい、3分間ページで走ってから気づいた。"""
    page, sock = _PageDrv(), _SocketDrv(last="z")
    monkeypatch.setattr(B, "DRIVER", page)
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: sock)
    assert B.ensure_driver() is sock, "ページドライバがあるとソケットを試さない"


def test_the_page_driver_is_kept_when_no_socket_can_be_had(monkeypatch):
    page = _PageDrv()
    monkeypatch.setattr(B, "DRIVER", page)
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: None)
    monkeypatch.setattr(B, "ensure_page_alive",
                        lambda: pytest.fail("生きているページドライバを作り直している"))
    assert B.ensure_driver() is page


# ---- ループを実際に回す ------------------------------------------------------------------------
#
# アクセサ単体の試験は全部緑のまま、ループ本体は `NameError: _cleaned` で落ちていた
# -- 置換で消した変数を後段の診断行がまだ参照していた。利用者には
# 「[bridge error: NameError...]」とだけ出た。単体で固めても、通して回さなければ意味がない。

def _handler():
    h = B.Handler.__new__(B.Handler)
    h.sent = []
    h._sse = lambda payload, event=None: h.sent.append((event, payload))
    h._ping = lambda: None
    return h


def test_the_streaming_loop_runs_end_to_end_on_a_socket(monkeypatch):
    drv = _SocketDrv(last="2")
    monkeypatch.setattr(B, "DRIVER", drv)
    monkeypatch.setattr(B, "ensure_driver", lambda: drv)
    monkeypatch.setattr(drv, "send", lambda msg, **kw: None, raising=False)
    monkeypatch.setattr(drv, "_is_generating", lambda: False, raising=False)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)

    out = _handler()._send_and_stream_once("1+1 は？")
    assert out == "2", out


def test_the_loop_streams_what_it_reads(monkeypatch):
    drv = _SocketDrv(last="答え")
    monkeypatch.setattr(B, "DRIVER", drv)
    monkeypatch.setattr(B, "ensure_driver", lambda: drv)
    monkeypatch.setattr(drv, "send", lambda msg, **kw: None, raising=False)
    monkeypatch.setattr(drv, "_is_generating", lambda: False, raising=False)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)

    h = _handler()
    h._send_and_stream_once("q")
    deltas = "".join((p or {}).get("delta", "") for _e, p in h.sent)
    assert "答え" in deltas, h.sent


def test_the_loop_runs_end_to_end_on_the_page_too(monkeypatch, dom):
    """ソケット側だけ通して満足すると、切り替えていない側が壊れたまま出ていく。"""
    dom["clean"], dom["lastmsg"] = "本文", "本文"
    drv = _PageDrv()
    drv._is_generating = lambda: False
    monkeypatch.setattr(B, "DRIVER", drv)
    monkeypatch.setattr(B, "ensure_driver", lambda: drv)
    monkeypatch.setattr(B, "_send_counted", lambda msg: None)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)

    assert _handler()._send_and_stream_once("q") == "本文"


def test_the_transport_decision_is_announced_where_it_can_be_seen():
    """このモジュールは logging を設定しない。root にハンドラが無いので INFO は捨てられる。
    経路が切り替わったかを logger.info で報せていて、実機確認が丸一往復むだになった。"""
    import inspect

    src = inspect.getsource(B.ensure_driver)
    # コメントを除いて見る。以前この検査を素の部分一致で書き、説明コメントの中の
    # 文字列に反応して落ちた（同じ罠をこのリポジトリで既に踏んでいる）。
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "logger.info" not in code, "届かない経路に出力している"
    assert "SOCKET" in code and "PAGE" in code
    assert code.count("print(") >= 2


# ---- 常駐ページの解放 --------------------------------------------------------------------------

def test_releasing_the_startup_page_clears_the_driver_with_it():
    """PAGE だけ、あるいは DRIVER だけ残すと、ensure_page_alive が直すために
    存在している壊れた状態を、わざわざ作って置いていくことになる。"""
    import inspect

    src = inspect.getsource(B._page_main)
    # 固定文字数で切らない。コメントを1行足しただけで窓から本体がはみ出し、
    # 検査が「無い」と言い出す（実際そうなった）。区切りは構造で取る。
    i = src.index("BRIDGE_RELEASE_STARTUP_PAGE")
    tail = src[i:src.index("PAGE_EXECUTOR.run_forever()", i)]
    assert "PAGE.close()" in tail
    assert "PAGE, DRIVER = None, None" in tail
    # 空ページを先に開くこと。最後のタブを閉じると Edge ごと終了し、
    # ブリッジはトークン捕捉にも再オープンにも要る context を失って自分も落ちた（実測）。
    assert tail.index("ctx.new_page()") < tail.index("PAGE.close()")
    # 既存の空ページを使い回すこと。毎回開くと再起動のたびに about:blank が増える
    # （2回目で2枚あった -- 漏れを塞ぐ修正が別の漏れを作っていた）。
    assert "about:blank" in tail and "ctx.pages" in tail


def test_the_release_requires_both_a_socket_and_a_known_agent_url():
    """ソケットが使えない、または起点URLが分からない状態でページを手放すと、
    次のターンが開くべき場所を知らないまま丸腰になる。"""
    import inspect

    src = inspect.getsource(B._page_main)
    i = src.index("BRIDGE_RELEASE_STARTUP_PAGE")
    line = src[i:src.index(":", i)]
    assert "BRIDGE_SOCKET" in line and "AGENT_URL" in line


def test_the_release_is_off_until_a_person_has_used_it():
    import inspect

    src = inspect.getsource(B)
    i = src.index("BRIDGE_RELEASE_STARTUP_PAGE = ")
    assert 'os.environ.get("MCP_BRIDGE_RELEASE_PAGE", "0")' in src[i:i + 200]


def test_startup_still_runs_everything_before_releasing():
    """サインイン検出・同意・自動再開はページを要る。解放はそれらの後でなければならない。"""
    import inspect

    src = inspect.getsource(B._page_main)
    assert src.index("_find_or_open_agent") < src.index("BRIDGE_RELEASE_STARTUP_PAGE")
    assert src.index("should_autoresume") < src.index("BRIDGE_RELEASE_STARTUP_PAGE")


# ---- 外部レビューで出た重大欠陥を、実挙動で縛る ------------------------------------------------

def test_partial_never_reaches_back_to_the_previous_answer(monkeypatch):
    """`partial_text() or read_last_response()` と書いていた。2ターン目の最初のポーリングで
    前回の回答が今回の delta として利用者に流れ、本物の partial が来た瞬間に継ぎ接ぎになる。"""
    monkeypatch.setattr(B, "DRIVER", _SocketDrv(partial="", last="前回の答え", answered=True))
    assert B._answer_partial() == ""


def test_a_dead_socket_does_not_block_the_way_back_to_the_page(monkeypatch):
    """経路が閉じた状態こそタブが要る場面なのに、そこでブリッジが固まっていた。"""
    dead = _SocketDrv(last="x")
    dead.failed = "ChatHubError: this socket route already failed"
    page = _PageDrv()
    monkeypatch.setattr(B, "DRIVER", dead)
    monkeypatch.setattr(B, "_bridge_socket_driver", lambda: None)
    monkeypatch.setattr(B, "run_on_page_thread", lambda fn, *a, **kw: fn(*a, **kw))

    def _ensure():
        B.DRIVER = page
        return True

    monkeypatch.setattr(B, "ensure_page_alive", _ensure)
    got = B.ensure_driver()
    assert got is page, "死んだソケットを返し続けている"
    assert not getattr(got, "failed", "")


def test_a_socket_turn_records_which_conversation_it_was_in(monkeypatch):
    """DOM差分はソケットのターンを見られない。会話IDはドライバが知っている。"""
    drv = _SocketDrv(last="a")
    drv.conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(B, "DRIVER", drv)
    ref = B.socket_conv_ref()
    assert B.sessref_guid(ref) == drv.conv_id


def test_a_socket_on_another_conversation_is_released(monkeypatch):
    drv = _SocketDrv(last="a")
    drv.conv_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(B, "DRIVER", drv)
    monkeypatch.setattr(B, "ACTIVE_SID", "sid1")
    monkeypatch.setattr(B.S, "load",
                        lambda sid: {"conv_url": B.make_sessref("22222222-2222-2222-2222-222222222222")})
    assert B.socket_is_on_the_active_conversation() is False
    assert B.release_socket_driver("test") is True
    assert B.DRIVER is None


def test_a_socket_on_the_same_conversation_is_kept(monkeypatch):
    drv = _SocketDrv(last="a")
    drv.conv_id = "33333333-3333-3333-3333-333333333333"
    monkeypatch.setattr(B, "DRIVER", drv)
    monkeypatch.setattr(B, "ACTIVE_SID", "sid1")
    monkeypatch.setattr(B.S, "load", lambda sid: {"conv_url": B.make_sessref(drv.conv_id)})
    assert B.socket_is_on_the_active_conversation() is True


def test_opening_a_fresh_conversation_releases_the_socket():
    """画面は新しいチャット、送信先は前の会話 -- 無言の誤配送。"""
    import inspect

    src = inspect.getsource(B._open_fresh_conversation)
    assert "release_socket_driver" in src


def test_switching_conversations_releases_the_socket():
    import inspect

    src = inspect.getsource(B.Handler._do_switch)
    assert "release_socket_driver" in src


# ---- 借りたページは返す ------------------------------------------------------------------------
#
# 解放したはずの常駐ページは、最初の /history で戻ってきて、そのまま居座っていた
# （実測 503MB → 1134MB）。DOM が要るのは1リクエストの間だけ。

class _Pg:
    def __init__(self, url="https://m365.cloud.microsoft/chat/agent/T_x", closed=False):
        self.url, self._closed = url, closed
        self.context = None

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True


class _Ctx:
    def __init__(self, pages):
        self.pages = list(pages)
        self.opened = 0

    def new_page(self):
        self.opened += 1
        pg = _Pg(url="about:blank")
        self.pages.append(pg)
        return pg


def _borrowable(monkeypatch, *, on_socket=True, existing=None, release=True):
    monkeypatch.setattr(B, "BRIDGE_RELEASE_STARTUP_PAGE", release)
    monkeypatch.setattr(B, "DRIVER", _SocketDrv(last="x") if on_socket else _PageDrv())
    monkeypatch.setattr(B, "CTX", object())
    monkeypatch.setattr(B, "PAGE", existing)

    def _ensure():
        if B.PAGE is None:
            pg = _Pg()
            pg.context = _Ctx([pg])
            B.PAGE = pg
        return True

    monkeypatch.setattr(B, "ensure_page_alive", _ensure)


def test_a_page_opened_for_one_request_is_given_back(monkeypatch):
    _borrowable(monkeypatch, existing=None)
    ok, mine = B.borrow_page()
    assert ok and mine is True
    page = B.PAGE
    assert B.return_page(mine) is True
    assert page.is_closed() and B.PAGE is None


def test_a_page_that_was_already_there_is_left_alone(monkeypatch):
    """自分が開けていないページを閉じるのは、他の誰かの足を払うこと。"""
    existing = _Pg()
    existing.context = _Ctx([existing])
    _borrowable(monkeypatch, existing=existing)
    ok, mine = B.borrow_page()
    assert ok and mine is False
    assert B.return_page(mine) is False
    assert not existing.is_closed() and B.PAGE is existing


def test_the_page_is_never_closed_when_it_IS_the_conversation(monkeypatch):
    """ソケットに乗っていなければ DRIVER はそのページのドライバで、
    閉じることは利用者の会話を終わらせること。節約とは釣り合わない。"""
    _borrowable(monkeypatch, on_socket=False, existing=None)
    ok, mine = B.borrow_page()
    assert ok and mine is True
    page = B.PAGE
    assert B.return_page(mine) is False
    assert not page.is_closed()


def test_nothing_is_returned_when_the_release_is_off(monkeypatch):
    _borrowable(monkeypatch, existing=None, release=False)
    ok, mine = B.borrow_page()
    assert B.return_page(mine) is False


def test_a_blank_page_is_left_holding_the_browser(monkeypatch):
    """最後のタブを閉じると Edge が終了する -- 起動時解放で一度踏んでいる。"""
    _borrowable(monkeypatch, existing=None)
    ok, mine = B.borrow_page()
    ctx = B.PAGE.context
    B.return_page(mine)
    assert ctx.opened == 1, "空ページを残さずに最後のタブを閉じている"


def test_every_borrowing_endpoint_gives_the_page_back():
    """1箇所でも返し忘れると、そこを一度通っただけで常駐が復活する。"""
    import inspect

    src = inspect.getsource(B.Handler.do_GET)
    assert src.count("borrow_page") == src.count("return_page") == 5
