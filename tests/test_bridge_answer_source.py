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
    IS_SOCKET = True

    def __init__(self, partial="", last="", generating=False):
        self._partial, self._last, self._gen = partial, last, generating

    def partial_text(self):
        return self._partial

    def settled_text(self):
        return "" if self._gen else self._last

    def read_last_response(self):
        return self._partial or self._last


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
