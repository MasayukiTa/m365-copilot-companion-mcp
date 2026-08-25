"""socket 経路が「速くなるだけ、壊れても仕事は落ちない」を守っているか。

この経路の前提は『Microsoft がいつ塞いでもおかしくない』こと。だからここで確かめるのは
速さではなく、塞がれた時に何が起きるか -- タブに戻る、戻り続けない、そして仕事は進む。
"""
import pytest

from relay import relay_fleet as rf
from relay import socket_route as _SR
from relay.socket_route import SocketRoute

#: The real default, read at import time -- before the autouse fixture below redirects it.
#: The one test that must check the SHIPPED path cannot read the patched one.
REAL_DEFAULT_LOG = _SR.DEFAULT_LOG


@pytest.fixture(autouse=True)
def _never_write_the_real_record(tmp_path, monkeypatch):
    """どのテストも本番の記録ファイルに触らせない。

    触れていた。close_route が記録を始めた瞬間、テスト由来の route_closed が
    .fleet/socket_route.jsonl に混ざり、『閉じていない経路が3回閉じた』という
    記録になった。学習データに合成行が混ざるのは、静かに間違いを教える。
    """
    from relay import socket_route as SR
    monkeypatch.setattr(SR, "DEFAULT_LOG", str(tmp_path / "isolated.jsonl"))


class _Tpl:
    gpt_id = "T_agent.x"


def _token(seconds=3600):
    """oid/tid/exp を持つだけの本物と同じ形。署名は使われない。"""
    import base64
    import json
    import time

    body = json.dumps({"oid": "o", "tid": "t", "exp": time.time() + seconds}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


def _route(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("connect_fn", object())
    return SocketRoute(**kw)


# ---- 既定は off -----------------------------------------------------------------------------

def test_the_route_can_still_be_switched_off():
    """既定は socket（2026-08-21〜）。だが1つの環境変数で全部タブに戻せること自体が
    この経路の安全装置なので、それが効くことを見張る。"""
    r = SocketRoute(enabled=False, capture_fn=lambda *a: (_token(), _Tpl()),
                    connect_fn=object())
    assert r.open() is False
    assert r.driver_for("w0") is None
    assert r.refresh(object(), "url") is False


# ---- 遮断器 ---------------------------------------------------------------------------------

def test_three_consecutive_failures_close_the_route_for_everyone():
    """1台ずつ順番に発見させると、その台数ぶん失敗ターンを払うことになる。"""
    r = _route()
    for i in range(3):
        assert r.open()
        r.note_failure("w%d: socket refused" % i)
    assert not r.open()
    assert "3 consecutive" in r.closed_reason


def test_a_success_clears_the_blip_counter():
    r = _route()
    r.note_failure("a")
    r.note_failure("b")
    r.note_success()
    r.note_failure("c")
    assert r.open()                    # 2連続で止まっている
    assert r.consecutive == 1


def test_a_route_that_half_works_is_stopped_by_the_one_way_counter():
    """成功を挟めば連続カウンタは消える。だが失敗のたびに『失敗ターン + タブを開く』を
    払うので、半分動く経路は経路が無いより悪い。総数カウンタは減らない。"""
    r = _route(max_fallbacks=4)
    for i in range(4):
        r.note_failure("w%d" % i)
        r.note_success()
    assert not r.open()
    assert "not reliable enough" in r.closed_reason
    assert r.fallbacks == 4


def test_closing_is_one_way():
    r = _route()
    r.close_route("microsoft closed it")
    for _ in range(10):
        r.note_success()
    assert not r.open()
    assert r.closed_reason == "microsoft closed it"


# ---- トークン -------------------------------------------------------------------------------

def test_a_capture_that_fails_does_not_raise_and_counts_as_a_failure():
    """捕獲が失敗する = 全員タブで走る。この経路が無かった時と同じ状態であって、事故ではない。"""
    def boom(_ctx, _url):
        raise RuntimeError("no composer")

    r = _route(capture_fn=boom)
    assert r.refresh(object(), "url") is False
    assert r.fallbacks == 1
    assert r.driver_for("w0") is None


def test_the_token_is_refreshed_before_it_expires_not_after():
    """期限切れを待つと、その瞬間に走っているワーカーが落ちてタブに戻る。
    早すぎて損をするのは安い捕獲1回だけ。"""
    calls = []

    def cap(_ctx, _url):
        calls.append(1)
        return _token(seconds=3000), _Tpl()

    r = _route(capture_fn=cap, refresh_margin_s=600)
    assert r.refresh(object(), "u") and len(calls) == 1
    assert r.needs_refresh() is False
    r.refresh(object(), "u")
    assert len(calls) == 1              # まだ余裕がある -> 捕獲しない

    r._entries[r.default_agent_url]["token"] = _token(seconds=100)   # 期限が迫った
    assert r.needs_refresh() is True


def test_a_running_conversation_sees_a_refreshed_token():
    """トークンではなく供給者を渡している。目標の途中で更新が起きても、
    走っている会話がそれを掴める。"""
    r = _route(capture_fn=lambda *a: (_token(), _Tpl()))
    r.refresh(object(), "u")
    drv = r.driver_for("w0")
    assert drv is not None
    first = drv.conv._token_supplier()
    r._entries[r.default_agent_url]["token"] = _token(seconds=7200)
    assert drv.conv._token_supplier() != first


def test_an_expired_token_hands_out_no_driver():
    r = _route(capture_fn=lambda *a: (_token(seconds=-10), _Tpl()))
    r.refresh(object(), "u")
    assert r.driver_for("w0") is None


def test_the_capture_tab_is_closed_again():
    """出発点が『生存確認のためにタブを開きっぱなし』だったので、
    トークンのためにタブを開きっぱなしにしたら同じ過ちを別の言い訳でやることになる。"""
    from relay import socket_route as SR

    closed = []

    class _Page:
        def close(self):
            closed.append(1)

    monkey = {}
    monkey["open"] = rf._open_fresh
    rf._open_fresh = lambda ctx, url: _Page()
    import relay.chathub_capture as CC
    orig = CC.capture
    CC.capture = lambda page, **kw: (_token(), _Tpl())
    try:
        tok, tpl = SR.capture_via_tab(object(), "url")
    finally:
        rf._open_fresh = monkey["open"]
        CC.capture = orig
    assert tok and tpl
    assert closed == [1]


# ---- 艦隊への配線 ---------------------------------------------------------------------------

class _FakeDrv:
    """タブ用ドライバの代役。`answers` はループが見る完了回答数。"""

    def __init__(self, answers=0):
        self.failed = ""
        self.closed = False
        self.answers = answers

    def _answers(self):
        return type("A", (), {"count": lambda s: self.answers})()

    def close(self):
        self.closed = True


def _worker(**kw):
    return rf.RelayWorker("do the thing", "w0", **kw)


def test_a_worker_takes_a_socket_when_one_is_offered_and_opens_no_tab(monkeypatch):
    drv = _FakeDrv()
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {
                            "driver_for": lambda self, n, **kw: drv})())
    monkeypatch.setattr(rf, "_open_fresh",
                        lambda *a: pytest.fail("a socket worker must not open a tab"))
    w = _worker()
    assert w.attach(object(), "agent-url") is True
    assert w.socket and w.drv is drv and w.page is None
    assert w.status == "ready"


def test_a_url_resume_is_never_given_a_socket(monkeypatch):
    """resume が URL なら『あのページをもう一度開く』という意味で、socket に開く URL は無い。

    以前この検査は「resume なら常に」だった。会話IDでの resume は実測(2026-08-24、対照群つき)
    で成立し、しかもタブを開かないほうが速く安いので、区別すべきは resume かどうかではなく
    「渡されたのがページか会話か」になった。URL 側の不変条件はそのまま残す。
    """
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: pytest.fail("a url resume must not even ask for a socket"))
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: _FakeDrv())
    w = _worker()
    w.resume_conv = "https://example/chat/123"
    assert w.attach(object(), "agent-url") is True
    assert w.socket is False


def test_a_socket_worker_reserves_disk_but_not_a_tab():
    """同じ eval を回すのでディスクは占める。タブは持たないので RAM は占めない。
    ここを一緒くたにすると、タブと無関係なディスク床に対して過剰入場する。"""
    w = _worker(max_research=0, refuter=False)
    w.socket = True
    assert w.tab_weight() == 0
    assert w.tab_load() == 0


def test_a_failed_socket_turn_reopens_as_a_tab_and_resends_the_same_job(monkeypatch):
    noted = []
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {
                            "driver_for": lambda self, n, **kw: None,
                            "note_failure": lambda self, why: noted.append(why),
                            "token_life": lambda self, agent_url=None: 1800.0,
                            "record": lambda self, event, **f: None})())
    tab = _FakeDrv()
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: tab)

    w = _worker()
    w._context, w._agent_url = object(), "agent-url"
    w.socket, w.drv = True, _FakeDrv()
    w.drv.failed = "ChatHubError: the backend declined the request: InvalidRequest"
    w.job = "the very same turn"
    w.status = "waiting"

    assert w._fall_back_to_tab() is True
    assert w.socket is False and w.drv is tab
    assert w.status == "ready"          # 'ready' が self.job を送る状態
    assert w.job == "the very same turn"
    assert noted and "InvalidRequest" in noted[0]


def test_a_socket_worker_closes_its_conversation_when_released():
    """タブに比べれば安いが無料ではない。仕事が終わった socket は閉じる。"""
    w = _worker()
    drv = _FakeDrv()
    w.socket, w.drv = True, drv
    w.close()
    assert drv.closed is True
    assert w.drv is None


# ---- 呼ばれ方まで見る（機能があるのに一度も実行されない、を防ぐ） ----------------------------

def test_poll_is_what_triggers_the_fallback(monkeypatch):
    """_fall_back_to_tab を直接呼ぶテストだけでは『実装したが誰も呼ばない』が通ってしまう。
    実際にそれをやって、5本のテストが全部緑のまま機能が一度も動かなかったことがある。"""
    called = []
    w = _worker()
    w.socket, w.drv = True, _FakeDrv()
    w.drv.failed = "socket gone"
    w.status = "waiting"
    monkeypatch.setattr(w, "_fall_back_to_tab", lambda: called.append(1) or True)
    w.poll()
    assert called == [1]


def test_a_healthy_socket_turn_does_not_fall_back(monkeypatch):
    w = _worker()
    w.socket, w.drv = True, _FakeDrv()      # failed == ""
    w.status = "waiting"
    monkeypatch.setattr(w, "_fall_back_to_tab",
                        lambda: pytest.fail("a working socket must not be dropped"))
    w.poll()


def test_a_tab_worker_is_unaffected_by_any_of_this(monkeypatch):
    w = _worker()
    w.page, w.drv, w.socket = object(), _FakeDrv(), False
    w.drv.failed = "this must be ignored: a tab driver has no such notion"
    w.status = "waiting"
    monkeypatch.setattr(w, "_fall_back_to_tab",
                        lambda: pytest.fail("a tab worker must never take the socket path"))
    w.poll()


# ---- 落ちた接続は、張り直す（タブに退避しない） ----------------------------------------------
#
# 2026-08-25 の実走行が出発点。31ターンがソケットで完了したあとに接続が3回切れ、そのたびに
# タブが開き、経路ごと遮断された。切断の理由は `ConnectionClosedError` -- 接続1本の故障で
# あって、その仕事がソケットで**できない**証拠ではない。にもかかわらず再接続を試みる経路が
# コードに存在しなかったので、「ソケットでは無理だった」はどの走行でも一度も測られていない。
# タブが買えたものは w5 DONE / w3 STUCK / w9 未完了 -- 3分の1、代償は常駐1.77GB。

DROPPED = "ConnectionClosedError: no close frame received or sent"


class _IdDrv(_FakeDrv):
    def __init__(self, answers=0, server="conv-42"):
        super().__init__(answers=answers)
        self._server = server

    def conversation_ids(self):
        return {"client": "", "server": self._server, "session": "s", "turns": 5}


def _retry_route(monkeypatch, *, drv, noted=None, records=None, is_open=True):
    """再接続を1本渡す経路の代役。`is_open` が False なら『経路は塞がれた』状態。"""
    asked = {}

    class _R:
        def open(self):
            return is_open

        def driver_for(self, n, **kw):
            # `_called` so a test that checks an argument was NOT passed cannot pass
            # vacuously by never reaching the call at all.
            asked.update(kw)
            asked["_called"] = True
            return drv

        def note_failure(self, why):
            (noted if noted is not None else []).append(why)

        def record(self, event, **f):
            (records if records is not None else []).append(dict(f, event=event))

        def token_life(self, agent_url=None):
            return 1800.0

    monkeypatch.setattr(rf, "_socket_route", lambda: _R())
    return asked


def test_a_dropped_connection_is_reconnected_and_no_tab_is_opened(monkeypatch):
    """これが本命。伝送故障でタブを開くのは、カードへの答えを接続断に出しているのと同じ。"""
    fresh = _FakeDrv()
    _retry_route(monkeypatch, drv=fresh)
    monkeypatch.setattr(rf, "_open_fresh",
                        lambda *a: pytest.fail("接続断でタブを開いてはいけない"))
    w = _worker()
    w.socket, w.drv = True, _FakeDrv()
    w.drv.failed = DROPPED
    w.job, w.status = "the very same turn", "waiting"

    w.poll()

    assert w.socket is True and w.drv is fresh
    assert w.status == "ready"                    # 'ready' が self.job を再送する状態
    assert w.job == "the very same turn"
    assert w._socket_fell_back is not True


def test_a_reconnect_is_not_counted_as_a_route_failure(monkeypatch):
    """経路を閉じたのはこの数え方。張り直しは経路の故障ではないので数えない。"""
    noted = []
    _retry_route(monkeypatch, drv=_FakeDrv(), noted=noted)
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("タブ不要"))
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w.drv.failed = DROPPED
    w.poll()
    assert noted == [], "再接続を経路の失敗として数えると、瞬断3回で経路が死ぬ"


def test_the_reconnect_continues_the_same_conversation(monkeypatch):
    """会話は接続より長生きする。新しい接続で新しい会話を始めたら、文脈を捨てたことになる。"""
    asked = _retry_route(monkeypatch, drv=_FakeDrv())
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("タブ不要"))
    w = _worker()
    w.socket, w.drv, w.status = True, _IdDrv(), "waiting"
    w.drv.failed = DROPPED
    w.poll()
    assert asked.get("conversation_id") == "conv-42"


def test_the_answer_counter_is_reset_so_later_successes_are_still_counted(monkeypatch):
    """新しいドライバの回答数は0から。古い値を残すと、追い越すまで成功が1件も数えられない。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("タブ不要"))
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(answers=5), "waiting"
    w._socket_turns_seen = 5
    w.drv.failed = DROPPED
    w.poll()
    assert w._socket_turns_seen == 0


def test_a_card_still_opens_a_tab_because_a_socket_cannot_show_one(monkeypatch):
    """全部を張り直しにすると、タブでしか解けない用件が永久に解けなくなる。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    tab = _FakeDrv()
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: tab)
    w = _worker()
    w._context, w._agent_url = object(), "agent-url"
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w.drv.failed = "the turn completed but carried no text (a card the tab can show?)"
    w.poll()
    assert w.socket is False and w.drv is tab


def test_an_unread_reason_is_not_assumed_transient(monkeypatch):
    """分類できない理由を再接続に回すと、分類機を静かに免罪して一覧が育たなくなる。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: _FakeDrv())
    w = _worker()
    w._context, w._agent_url = object(), "u"
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w.drv.failed = "ChatHubError: the backend declined the request: InvalidRequest"
    w.poll()
    assert w.socket is False, "unknown は route 扱いにしない"


def test_an_exhausted_retry_budget_reports_to_the_route_and_keeps_the_socket(monkeypatch):
    """経路が開いている限りソケットに留まる。ただし黙って粘るのではなく、
    使い切った予算を1件の失敗として経路に報告する -- それが遮断の判断材料になる。"""
    noted = []
    _retry_route(monkeypatch, drv=_FakeDrv(), noted=noted, is_open=True)
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("経路が開いていればタブは開かない"))
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w._socket_retries = rf.DEFAULT_SOCKET_RETRIES
    w.drv.failed = DROPPED
    w.poll()
    assert len(noted) == 1
    assert w.socket is True and w._socket_retries == 1


def test_a_closed_route_is_what_sends_the_worker_to_a_tab(monkeypatch):
    """タブに退避する条件はただ一つ、経路そのものが塞がれたこと。
    本当に塞がれた時に仕事が止まっては、退避路の意味がない。"""
    _retry_route(monkeypatch, drv=_FakeDrv(), is_open=False)
    tab = _FakeDrv()
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: tab)
    w = _worker()
    w._context, w._agent_url = object(), "u"
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w._socket_retries = rf.DEFAULT_SOCKET_RETRIES
    w.drv.failed = DROPPED
    w.poll()
    assert w.socket is False and w.drv is tab


def test_a_route_that_hands_out_no_driver_falls_back_rather_than_hanging(monkeypatch):
    """トークン失効・経路遮断なら driver_for は None を返す。そこで止まったら仕事が消える。"""
    _retry_route(monkeypatch, drv=None)
    tab = _FakeDrv()
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: tab)
    w = _worker()
    w._context, w._agent_url = object(), "u"
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w.drv.failed = DROPPED
    w.poll()
    assert w.socket is False and w.drv is tab


def test_the_refresh_margin_exceeds_the_longest_turn_a_worker_can_take():
    """更新猶予と最長ターンが同値だった。残り601秒で始めて600秒走れば、終わる直前に失効する。

    ワーカーは turn_timeout_s を渡さないので driver_for の既定がそのまま最長ターンになる。
    ヘッダは接続時に一度しか読まないため、走行中のターンは新トークンの恩恵を受けない。
    """
    import inspect

    module_default = inspect.signature(
        SocketRoute.driver_for).parameters["turn_timeout_s"].default
    longest = max(rf.SOCKET_TURN_TIMEOUT_S, module_default)
    assert rf.SOCKET_REFRESH_MARGIN_S > longest, (
        "更新猶予 %.0fs が最長ターン %.0fs を上回っていない"
        % (rf.SOCKET_REFRESH_MARGIN_S, longest))


def test_a_worker_turn_is_given_more_than_the_modules_ten_minute_default():
    """2026-08-25 20:12 の走行では、再接続の理由が4回とも
    「turn deadline exceeded」で、トークンは 3705/3530/2477/3103 秒残っていた。
    落としていたのは資格情報ではなくこの時計だった。"""
    import inspect

    module_default = inspect.signature(
        SocketRoute.driver_for).parameters["turn_timeout_s"].default
    assert rf.SOCKET_TURN_TIMEOUT_S > module_default


def test_every_worker_socket_is_opened_with_that_deadline():
    """1箇所でも渡し忘れると、その経路だけ 600 秒に戻って同じ所で落ちる。"""
    import inspect

    import re

    src = inspect.getsource(rf)
    sites = re.findall(r"driver_for\(\s*self\.name[^)]*\)", src, re.S)
    assert len(sites) >= 3, "driver_for(self.name...) の呼び出しを見失っている"
    for site in sites:
        assert "SOCKET_TURN_TIMEOUT_S" in site, "期限を渡していない呼び出し: %s" % site[:80]


def test_the_fleet_actually_hands_its_margin_to_the_route(monkeypatch):
    """定数を置いただけで経路に渡していなければ、猶予は 600 のままになる。"""
    import relay.socket_route as SR

    seen = {}
    monkeypatch.setattr(rf, "_SOCKET_ROUTE", None)
    monkeypatch.setattr(SR, "SocketRoute",
                        lambda **kw: seen.update(kw) or object())
    rf._socket_route()
    assert seen.get("refresh_margin_s") == rf.SOCKET_REFRESH_MARGIN_S


# ---- 会話リサイクルは socket にも要る ---------------------------------------------------------
#
# 2026-08-25 19:47、実走行で発見。トークン上限に達した socket ワーカーが
# `self.page.goto(...)` に入り、page は None なので AttributeError、それを裸の except が
# landed=False に変え、「fresh conversation did not render」で STUCK。DOM の話を、DOM を
# 持たない経路について報告していた。同じゴールが再試行でも同じ所で落ちた。

EXHAUSTED = "OpenAIModelTokenLimit: the conversation has exceeded the token limit"


def test_a_socket_worker_recycles_into_a_fresh_conversation_without_a_page(monkeypatch):
    fresh = _FakeDrv()
    _retry_route(monkeypatch, drv=fresh)
    w = _worker()
    w.socket, w.drv, w.page = True, _FakeDrv(answers=3), None
    w._socket_turns_seen = 3
    w._decide(EXHAUSTED)
    assert w.outcome != "STUCK", "socket ワーカーがリサイクルできずに STUCK になっている"
    assert w.drv is fresh
    assert w._socket_turns_seen == 0


def test_the_recycle_asks_for_a_conversation_id_free_driver(monkeypatch):
    """会話IDを渡してしまうと、作り直したい当の会話に戻ってしまう。"""
    asked = _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.page = True, _FakeDrv(), None
    w._decide(EXHAUSTED)
    assert asked.get("_called"), "リサイクル分岐に入っていない（検査が空振りしている）"
    assert not asked.get("conversation_id"), "リサイクルが同じ会話を継続しようとしている"


def test_a_socket_recycle_that_fails_says_so_in_the_transport_it_failed_in(monkeypatch):
    _retry_route(monkeypatch, drv=None)
    w = _worker()
    w.socket, w.drv, w.page = True, _FakeDrv(), None
    w._decide(EXHAUSTED)
    assert w.outcome == "STUCK"
    assert "render" not in w.reason, "DOM の無い経路に DOM の理由を付けている"


def test_a_tab_worker_still_reloads_its_page(monkeypatch):
    """socket 側を足したせいでタブ側の道を壊していないこと。"""
    seen = {}

    class _Page:
        def goto(self, url, **kw):
            seen["url"] = url

        def wait_for_timeout(self, ms):
            pass

        def locator(self, sel):
            return type("L", (), {"count": lambda s: 1})()

    w = _worker()
    w.socket, w.page, w.drv = False, _Page(), _FakeDrv()
    w._agent_url = "https://agent.example/chat"
    w._decide(EXHAUSTED)
    assert seen.get("url") == "https://agent.example/chat"
    assert w.outcome != "STUCK"



# ---- ブラウザを作り直したら、経路も作り直す -----------------------------------------------------
#
# 2026-08-25 20:07 実測。watchdog が 150 秒の停滞で Edge を hard-reset し、その直後から
# capture が TargetClosedError で3回失敗して経路が恒久遮断。以後の recover は、復旧させたい
# 当のものがトークンを作れないので全部無駄撃ちになり、64ターンをソケットで運んだ走行が終了した。
# 「一度閉じた経路は開かない」はバックエンドが拒み始めた場合の規則であって、
# 自分のブラウザを自分で消した場合には当てはまらない。

def test_resetting_the_edge_discards_the_route_bound_to_it(monkeypatch):
    first = rf._socket_route()
    assert rf._socket_route() is first, "毎回作り直していては singleton の意味がない"
    rf.reset_socket_route()
    assert rf._SOCKET_ROUTE is None
    assert rf._socket_route() is not first, "古いブラウザのトークンを持った経路が残っている"


def test_every_edge_hard_reset_in_the_runner_forgets_the_route():
    """呼び出し側が1箇所でも素の hard_reset を呼べば、そこだけ同じ壊れ方をする。"""
    import inspect

    from relay import fleet_runner as FR

    src = inspect.getsource(FR)
    assert "reset_socket_route" in src, "runner が経路を捨てていない"
    # 素の import 名を残したまま使うと、この包み込みを迂回できてしまう
    assert "hard_reset as _edge_hard_reset" in src
    # 署名でアンカーしない。`discretionary` を足した時にここが ValueError で落ちた
    # -- 検査したいのは引数の形ではなく「素の hard_reset を呼んでいないこと」。
    body = src[src.index("def hard_reset(port"):]
    assert "_edge_hard_reset(port)" in body and "reset_socket_route()" in body


# ---- 二つの会計 -------------------------------------------------------------------------------

class _SidePage:
    page = object()

    def close(self):
        pass


def test_side_pages_are_counted_for_both_kinds_of_worker():
    """socket ワーカーも research/refuter の副タブは開く。主タブが無いから 0、では
    まさにこの会計が縛ろうとしているタブを数え落とす。"""
    tab = _worker()
    tab.page = object()
    tab._research_session = _SidePage()
    assert tab.tab_load() == 2                    # 主タブ + 副タブ

    sock = _worker()
    sock.socket = True
    sock._research_session = _SidePage()
    assert sock.tab_load() == 1                   # 主タブは無いが副タブはある


def test_the_disk_slot_counts_a_socket_worker_and_the_tab_slot_does_not():
    sock = _worker(max_research=0, refuter=False)
    sock.socket, sock.status = True, "waiting"
    assert rf._holds_slot(sock) is True           # 同じ eval を回す = ディスクを占める
    assert sock.tab_weight() == 0                 # タブは持たない = RAM を占めない

    sock.status = "done"
    assert rf._holds_slot(sock) is False          # 終わったものは何も占めない

    idle = _worker()
    assert rf._holds_slot(idle) is False          # まだ入場していない


def test_turns_that_worked_are_reported_so_the_breaker_can_reset(monkeypatch):
    """遮断器が失敗しか聞かされていないと、consecutive が 0 に戻らない。
    何千ターン成功していても、何時間かに散らばった3回の失敗で経路が閉じる。"""
    r = SocketRoute(enabled=True, connect_fn=object())
    monkeypatch.setattr(rf, "_socket_route", lambda: r)

    class _Drv(_FakeDrv):
        n = 0

        def _answers(self):
            return type("A", (), {"count": lambda s: _Drv.n})()

    w = _worker()
    w.socket, w.drv, w.status = True, _Drv(), "waiting"

    r.note_failure("a")
    r.note_failure("b")
    assert r.consecutive == 2

    _Drv.n = 1                       # 1ターン成功した
    w.poll()
    assert r.consecutive == 0 and r.turns == 1

    _Drv.n = 3                       # 見ていない間に2ターン進んだ
    w.poll()
    assert r.turns == 3
    w.poll()
    assert r.turns == 3              # 同じターンを二度数えない


def test_a_failed_turn_is_not_reported_as_a_success(monkeypatch):
    r = SocketRoute(enabled=True, connect_fn=object())
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    w = _worker()
    # 1ターン成功したあとに落ちたドライバ。回答数は 1 のまま増えない --
    # 失敗を成功として数えないのは、この構造そのものが担保している。
    w.socket, w.drv, w.status = True, _FakeDrv(answers=1), "waiting"
    w._socket_turns_seen = 1
    w.drv.failed = "gone"
    monkeypatch.setattr(w, "_fall_back_to_tab", lambda: True)
    w.poll()
    assert r.turns == 0


# ---- 分類機の学習データ（B0） -----------------------------------------------------------------
#
# 分類機は「どの依頼がタブを要るか」を当てるもの。その正解ラベルは、実際に fallback した
# 記録以外から作れない。print は実行が終われば消えるので、消えない場所に残す。

def _route_logging(tmp_path, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("connect_fn", object())
    kw.setdefault("log_path", str(tmp_path / "socket_route.jsonl"))
    return SocketRoute(**kw)


def _lines(tmp_path):
    import json
    p = tmp_path / "socket_route.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_record_survives_the_process_that_wrote_it(tmp_path):
    r = _route_logging(tmp_path)
    r.record("fallback", worker="w0", goal="請求書を集計して", reason="InvalidRequest")
    rows = _lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "fallback"
    assert rows[0]["goal"] == "請求書を集計して"
    assert rows[0]["reason"] == "InvalidRequest"
    assert rows[0]["ts"] > 0 and rows[0]["at"]


def test_records_append_rather_than_replace(tmp_path):
    r = _route_logging(tmp_path)
    r.record("fallback", worker="w0")
    r.record("worker_done", worker="w1")
    assert [x["event"] for x in _lines(tmp_path)] == ["fallback", "worker_done"]


def test_a_disabled_route_writes_nothing_at_all(tmp_path):
    """フラグ off の艦隊は、今まで書いていなかったファイルを書き始めてはならない。"""
    r = _route_logging(tmp_path, enabled=False)
    r.record("fallback", worker="w0")
    assert not (tmp_path / "socket_route.jsonl").exists()


def test_a_record_that_cannot_be_written_costs_nothing(tmp_path):
    """記録はターンより優先されない。この経路の仕事はタブより安いことであって、
    ログ基盤であることではない。"""
    r = _route_logging(tmp_path, log_path=str(tmp_path / "no" / "such" / "\0" / "x.jsonl"))
    r.record("fallback", worker="w0")          # 例外を出さないこと自体が要件


def test_closing_the_route_is_itself_recorded(tmp_path):
    r = _route_logging(tmp_path)
    r.close_route("microsoft closed it")
    rows = [x for x in _lines(tmp_path) if x["event"] == "route_closed"]
    assert rows and rows[0]["reason"] == "microsoft closed it"


def test_the_default_record_goes_somewhere_git_does_not_publish():
    """記録には目標文が入る。目標文は利用者の仕事であって、公開するものではない。"""
    import subprocess
    assert ".fleet" in REAL_DEFAULT_LOG.replace("\\", "/")
    r = subprocess.run(["git", "check-ignore", "-q", REAL_DEFAULT_LOG],
                       capture_output=True)
    assert r.returncode == 0, "DEFAULT_LOG is not gitignored"


def test_a_fallback_records_the_goal_beside_the_reason(monkeypatch, tmp_path):
    """分類機が要るのは『どの依頼が』『なぜ』タブを必要としたかの対。
    片方だけでは学習データにならない。"""
    r = _route_logging(tmp_path)
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: _FakeDrv())

    w = rf.RelayWorker("コネクタの承認が要る作業をして", "w0")
    w._context, w._agent_url = object(), "u"
    w.socket, w.drv = True, _FakeDrv()
    w.drv.failed = "the turn completed but carried no text (a card the tab can show?)"
    assert w._fall_back_to_tab() is True

    row = next(x for x in _lines(tmp_path) if x["event"] == "fallback")
    assert row["goal"] == "コネクタの承認が要る作業をして"
    assert "carried no text" in row["reason"]
    assert w._socket_fell_back is True


def test_a_goal_that_never_needed_a_tab_is_recorded_too(monkeypatch, tmp_path):
    """失敗だけを記録すると、分類機は『全部失敗する』を学ぶ。"""
    r = _route_logging(tmp_path)
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    w = rf.RelayWorker("ツールの総数を数えて", "w1")
    w.socket, w.drv = True, _FakeDrv(answers=2)
    w.turn, w.outcome, w.status = 2, "DONE", "done"
    w.close()

    row = next(x for x in _lines(tmp_path) if x["event"] == "worker_done")
    assert row["route"] == "socket" and row["fell_back"] is False
    assert row["outcome"] == "DONE" and row["goal"] == "ツールの総数を数えて"


def test_a_worker_that_fell_back_is_labelled_as_having_needed_a_tab(monkeypatch, tmp_path):
    """`socket` は fallback 後に False になるので、それだけでは
    『この目標はどちらの経路を必要としたか』に答えられない。"""
    r = _route_logging(tmp_path)
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    w = rf.RelayWorker("承認カードが出る作業", "w2")
    w.socket, w._socket_fell_back = False, True
    w.drv, w.outcome, w.status = _FakeDrv(), "DONE", "done"
    w.close()

    row = next(x for x in _lines(tmp_path) if x["event"] == "worker_done")
    assert row["route"] == "tab" and row["fell_back"] is True


def test_a_long_goal_is_truncated_by_the_caller_not_by_the_record(monkeypatch, tmp_path):
    """1件が会話まるごとになると、記録はすぐ読めない大きさになる。
    切るのは呼び出し側 -- record() は渡されたものをそのまま書く道具に留める。"""
    r = _route_logging(tmp_path)
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    w = rf.RelayWorker("あ" * 5000, "w9")
    w.socket, w.drv, w.outcome, w.status = True, _FakeDrv(), "DONE", "done"
    w.close()
    assert len(next(x for x in _lines(tmp_path) if x["event"] == "worker_done")["goal"]) == 600


def test_no_test_in_this_file_can_reach_the_real_record():
    """上の autouse fixture が効いていることを、明示的に1本で見張る。
    効かなくなっても他のテストは緑のままなので、これが唯一の警報になる。"""
    from relay import socket_route as SR
    r = SocketRoute(enabled=True, connect_fn=object())
    assert ".fleet" not in r.log_path.replace("\\", "/")
    assert r.log_path == SR.DEFAULT_LOG


# ---- 複数エージェント（A: 副エージェントの socket 化） ---------------------------------------
#
# 艦隊は1つのエージェントとだけ話しているわけではない。実装エージェント (T_...) と
# Researcher (P_....dr_work) は別物で、テンプレートは自分のエージェントをフレームに書く。
# 共有テンプレート1つだと、research のターンが黙って別のエージェントに届く -- そして
# そのエージェントは普通に答えてしまうので、間違いが見えない。

class _Tpl2:
    def __init__(self, gpt_id, model="Default"):
        self.gpt_id = gpt_id
        self._model = model
        self.applied = None

    def with_deep_research_model(self, m):
        out = _Tpl2(self.gpt_id, m)
        out.applied = m
        return out


def test_two_agents_get_two_templates(tmp_path):
    made = {}

    def cap(_ctx, url):
        made[url] = made.get(url, 0) + 1
        return _token(), _Tpl2("T_impl" if "impl" in url else "P_dr")

    r = _route_logging(tmp_path, capture_fn=cap)
    assert r.refresh(object(), "https://x/impl")
    assert r.refresh(object(), "https://x/researcher")
    assert r.driver_for("w", agent_url="https://x/impl").conv.template.gpt_id == "T_impl"
    assert r.driver_for("w", agent_url="https://x/researcher").conv.template.gpt_id == "P_dr"


def test_the_first_agent_captured_is_what_an_unnamed_caller_gets(tmp_path):
    """既存の呼び出し側は agent_url を渡さない。黙って別のエージェントに行かせない。"""
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2("T_impl")))
    r.refresh(object(), "https://x/impl")
    r._entries["https://x/researcher"] = {"token": _token(), "template": _Tpl2("P_dr")}
    assert r.default_agent_url == "https://x/impl"
    assert r.driver_for("w").conv.template.gpt_id == "T_impl"


def test_an_agent_never_captured_hands_out_no_driver(tmp_path):
    """知らないエージェントを既定で代用すると、まさに防ぎたい取り違えになる。"""
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2("T_impl")))
    r.refresh(object(), "https://x/impl")
    assert r.driver_for("w", agent_url="https://x/never-seen") is None


def test_each_agent_keeps_its_own_token(tmp_path):
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2("g")))
    r.refresh(object(), "a")
    r.refresh(object(), "b")
    r._entries["a"]["token"] = _token(seconds=-1)
    assert r.driver_for("w", agent_url="a") is None      # 期限切れ
    assert r.driver_for("w", agent_url="b") is not None   # 巻き添えにしない


def test_refreshing_one_agent_does_not_refresh_another(tmp_path):
    calls = []
    r = _route_logging(tmp_path,
                       capture_fn=lambda _c, u: (calls.append(u), (_token(), _Tpl2("g")))[1])
    r.refresh(object(), "a")
    r.refresh(object(), "a")           # まだ余裕がある
    r.refresh(object(), "b")
    assert calls == ["a", "b"]


def test_the_model_is_applied_to_a_copy_not_the_stored_template(tmp_path):
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2("P_dr")))
    r.refresh(object(), "res")
    drv = r.driver_for("w", agent_url="res", model="Claude")
    assert drv.conv.template.applied == "Claude"
    assert r.template_for("res").applied is None, "共有テンプレートを書き換えている"


def test_a_research_turn_gets_its_own_patience(tmp_path):
    """10分考えるのは research では普通で、chat ではハング。同じ上限は使えない。"""
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2("P_dr")))
    r.refresh(object(), "res")
    drv = r.driver_for("w", agent_url="res", turn_timeout_s=1800, frame_timeout_s=300)
    assert drv.conv.turn_timeout_s == 1800
    assert drv.conv.frame_timeout_s == 300


# ---- 既定 on（2026-08-21〜） -----------------------------------------------------------------

def test_the_default_is_the_socket(monkeypatch):
    """反転を正当化したのは速さでもメモリでもなく、**失敗が観測されたこと**。
    socket レビューが期限超過でページに落ち、正しい判定を返し、理由が記録された。
    それまで復旧経路は主張でしかなく、安全に失敗するところを見ていない経路を
    既定にはできない。"""
    import importlib

    from relay import socket_route as SR
    monkeypatch.delenv("MCP_FLEET_SOCKET", raising=False)
    assert importlib.reload(SR).ENABLED is True


def test_one_variable_puts_everything_back_on_tabs(monkeypatch):
    import importlib

    from relay import socket_route as SR
    for off in ("0", "off", "false", "no", "OFF", ""):
        monkeypatch.setenv("MCP_FLEET_SOCKET", off)
        assert importlib.reload(SR).ENABLED is False, off


def test_an_explicit_blank_is_off_and_an_absent_variable_is_on(monkeypatch):
    """スクリプトは空文字を代入して無効化する。`get(key, "1")` では
    『空を代入した』と『そもそも無い』が区別できず、無効化が黙って効かなくなる。"""
    import importlib

    from relay import socket_route as SR
    monkeypatch.setenv("MCP_FLEET_SOCKET", "")
    assert importlib.reload(SR).ENABLED is False
    monkeypatch.delenv("MCP_FLEET_SOCKET", raising=False)
    assert importlib.reload(SR).ENABLED is True


# ---- 二重の締め切り（実測 2026-08-21、12目標の並列走行より） ---------------------------------
#
# 実測: 12件中11件 DONE、fallback 0。唯一の失敗は最も重い目標（DB + skill_match）で、
# socket のターンが 206 秒時点でまだ正常に動いていたのに、艦隊側のタブ時代の
# 生成待ち予算が先に尽きて STUCK になった。
#
# タブ用の予算が要るのは「詰まったタブは詰まったと言わない」から。socket は言う --
# turn_timeout_s があり、超えれば例外を投げ、ドライバの failed がタブへの切替を起こす。
# 1つのターンに締め切りが2つあるのは1つ多い。

class _GenDrv2(_FakeDrv):
    def __init__(self, generating):
        super().__init__()
        self.generating = generating

    def _is_generating(self):
        return self.generating


def _deferring_worker(drv, socket=True):
    import time as _t
    w = _worker()
    w.socket, w.drv = socket, drv
    w.first_defer_ts = _t.time() - 10_000      # 予算はとうに尽きている
    w.gen_waits = 10_000                       # 回数の方も尽きている
    w.max_gen_wait_s = 1.0
    w.max_gen_waits = 1
    w._defer_progress_sig = 5                  # 進捗も平坦（socket は途中で何も出さないことがある）
    return w


def test_a_working_socket_turn_is_not_killed_by_the_tab_era_budget():
    w = _deferring_worker(_GenDrv2(generating=True))
    assert w._defer_generation() is True, "健全な socket ターンを予算切れで殺している"
    assert w.status == "ready"


def test_a_socket_turn_that_has_stopped_falls_back_to_the_normal_rules():
    """生成が止まっているのに待ち続けるのは、ただのハングになる。"""
    w = _deferring_worker(_GenDrv2(generating=False))
    assert w._defer_generation() is False


def test_a_tab_worker_keeps_the_budget_it_always_had():
    """タブは詰まったことを自分から言わない。だから予算はタブのために残す。"""
    w = _deferring_worker(_GenDrv2(generating=True), socket=False)
    assert w._defer_generation() is False


def test_a_driver_that_cannot_answer_does_not_get_infinite_patience():
    class _Broken(_FakeDrv):
        def _is_generating(self):
            raise RuntimeError("gone")

    w = _deferring_worker(_Broken())
    assert w._defer_generation() is False


def test_two_workers_do_not_capture_at_the_same_time(tmp_path):
    """実測 2026-08-21、24目標・並列6: タブが基準1+捕獲1=2 のはずが peak 3 だった。
    refresh は needs_refresh を読んでから捕獲するので、2人が同時に通れる。
    捕獲2本 = 一時メモリ2倍 + 1つのトークンを知るための実ターン2回。"""
    import threading

    started, done = [], threading.Event()

    def slow_capture(_ctx, url):
        started.append(url)
        done.wait(2.0)                      # 1本目が中にいる間に2本目が来る
        return _token(), _Tpl2("T_impl")

    r = _route_logging(tmp_path, capture_fn=slow_capture)
    t = threading.Thread(target=lambda: r.refresh(object(), "a"), daemon=True)
    t.start()
    while not started:
        import time as _t
        _t.sleep(0.01)
    second = {"ok": None}
    t2 = threading.Thread(target=lambda: second.__setitem__("ok", r.refresh(object(), "a")),
                          daemon=True)
    t2.start()
    done.set()
    t.join(timeout=5)
    t2.join(timeout=5)
    assert len(started) == 1, "同じエージェントの捕獲が2本走った"
    assert second["ok"] is True, "待った側はトークンを見つけて成功で返るべき"


def test_two_agents_can_still_capture_at_the_same_time(tmp_path):
    """直列化するのは同じエージェントだけ。別エージェントまで待たせると、
    2種類のトークンを揃えるのに順番待ちの時間が積み上がる。"""
    r = _route_logging(tmp_path, capture_fn=lambda _c, u: (_token(), _Tpl2(u)))
    assert r.refresh(object(), "a") is True
    assert r.refresh(object(), "b") is True
    assert r.template_for("a").gpt_id == "a"
    assert r.template_for("b").gpt_id == "b"


# ---- unlock が一度も送られていなかった件（Fable の調査 + 実物確認、2026-08-21） --------------
#
# 連鎖は3本だった:
#   1. _decide の unlock 分岐が self.job を組み立てて status を設定しない -> 送信されない
#   2. 送信されないので次の掃引が同じ回答を読み直し、また locked と判定して試行を消費する
#   3. その回答は 533 文字で marker 分岐の上限を超えており、判定していたのは
#      「誰かの拒否が最近あった」だけを見る fallback 分岐だった（並列時は他人の拒否）
# 結果、4回の試行が約8秒で尽き、「IPが変わる/パスワード不一致」という
# **実際には起きていない原因**を名指しするメッセージが出ていた。

def test_the_unlock_job_is_actually_sent():
    """job を組み立てても 'ready' にしなければ送信されない。
    この1行が無いために、unlock は一度もエージェントに届いていなかった。"""
    import relay.relay_fleet as rf
    w = _worker()
    w._turn_sent_at = 1.0
    w._unlock_attempts = 0
    orig_looks, orig_pw = rf._looks_locked, rf._unlock_password
    rf._looks_locked = lambda resp, since=0.0: True
    rf._unlock_password = lambda: "pw"
    try:
        w._decide("[locked: unlock required]")
    finally:
        rf._looks_locked, rf._unlock_password = orig_looks, orig_pw
    assert w.status == "ready", "unlock ジョブが送信される状態になっていない"
    assert "unlock" in w.job and w._unlock_attempts == 1


def test_a_long_ordinary_answer_is_not_a_lock_error(tmp_path, monkeypatch):
    """並列実行では、拒否の記録は誰のものか分からないまま共有される。
    533文字の会議要約が『ロック』と判定されたのがこれ。

    boolean を差し替えるのではなく、実際に拒否を記録して判定させる -- 実装が
    「はい/いいえ」から「どの記録か」に変わったとき、差し替え型のテストは
    実装より先に古くなり、通らなくなって初めてそれに気づく。"""
    import relay.relay_fleet as rf
    LS = _lock_tmp(tmp_path, monkeypatch)
    LS.record_locked("203.0.113.7", "refused", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)

    long_answer = "会議の要約です。" * 80           # マーカー無し・長い
    assert len(long_answer) >= rf.LOCKED_DOMINANCE_MAX_CHARS
    assert rf._looks_locked(long_answer, since=99.0) is False
    short_paraphrase = "unlock パスワード欠如で確定。STUCK。"
    assert rf._looks_locked(short_paraphrase, since=99.0) is True


def _lock_tmp(tmp_path, monkeypatch):
    from pathlib import Path

    import tools.lock_state as LS
    monkeypatch.setattr(LS, "_LOG_FILE", Path(str(tmp_path / "lock_refusals.jsonl")))
    monkeypatch.setattr(LS, "_STATE_FILE", Path(str(tmp_path / "lock_state.json")))
    return LS


def _rows(tmp_path):
    import json
    p = tmp_path / "lock_refusals.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_the_fallback_records_which_refusal_it_acted_on(tmp_path, monkeypatch):
    """『ロックだ』と判定したのに、その根拠がどこにも残らないのが前回の行き止まり。
    fallback は他人が書いた記録を消費するので、どれを読んだかが要る。"""
    import relay.relay_fleet as rf
    LS = _lock_tmp(tmp_path, monkeypatch)
    LS.record_locked("203.0.113.7", "refused", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert rf._looks_locked("unlock 未提供で STUCK。", since=99.0) is True
    row = [r for r in _rows(tmp_path) if r.get("event") == "classified_locked"][-1]
    assert row["branch"] == "fallback"
    assert row["consumed"]["client_ip"] == "203.0.113.7"
    assert row["turn_sent_at"] == 99.0


def test_the_marker_branch_says_it_was_the_marker(tmp_path, monkeypatch):
    """どちらの規則が発火したかで、次に疑う場所が変わる。"""
    import relay.relay_fleet as rf
    _lock_tmp(tmp_path, monkeypatch)
    assert rf._looks_locked("[locked client IP: '203.0.113.7'] write refused", since=0.0) is True
    row = [r for r in _rows(tmp_path) if r.get("event") == "classified_locked"][-1]
    assert row["branch"] == "marker"
    assert row["consumed"] == {}


def test_a_reply_that_is_not_locked_records_nothing(tmp_path, monkeypatch):
    """発火しなかった判定まで書くと、読む人がいなくなる。"""
    import relay.relay_fleet as rf
    _lock_tmp(tmp_path, monkeypatch)
    assert rf._looks_locked("ふつうの回答です。", since=99.0) is False
    assert [r for r in _rows(tmp_path) if r.get("event") == "classified_locked"] == []


def test_the_pinned_prefix_still_matches_what_security_actually_writes():
    """フィルタはこのリテラルの一致にしか支えられていない。凍結ファイル側が
    文言を変えたら、フィルタは黙って効かなくなる -- だから文字列そのものを見張る。"""
    import io as _io

    import relay.relay_fleet as rf
    src = _io.open("tools/security.py", encoding="utf-8").read()
    assert rf.NO_CONTEXT_REFUSAL in src, "security.py の文言と食い違っている"


def test_a_blank_ip_from_a_real_request_is_still_treated_as_a_lock(tmp_path, monkeypatch):
    """最初の版は client_ip の空白をキーにしていて、それは穴だった。

    derive_identity は X-Forwarded-For が区切り文字だけの実リクエストに対して空の識別子を
    返す。そのヘッダは呼び出し元が付ける。つまり遠隔の呼び出し元が空 IP の拒否を好きなだけ
    作れて、この分岐を無効化できた -- 見える STUCK が、ロック下で作られた気づかれない
    回答に変わる。直そうとしたバグより悪い。"""
    import relay.relay_fleet as rf
    LS = _lock_tmp(tmp_path, monkeypatch)
    LS.record_locked("", "[locked client IP: ''] Mutating and execution tools require...",
                     ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert rf._looks_locked("unlock 未提供で STUCK。", since=99.0) is True


def test_a_refusal_with_no_client_is_not_this_workers_lock(tmp_path, monkeypatch):
    """security.py はコンテキストの無い呼び出し元を拒否し、client_ip を空で記録する --
    それは in-process の誰かであって、遠隔ワーカーではない。ワーカーは必ず
    コンテキストを持つので、空 IP の記録は必ず他人のもの。

    実測 2026-08-21: relay が毎ターン踏むこの拒否が、並行するワーカーの
    自動 unlock 試行4回を溶かしていた。"""
    import relay.relay_fleet as rf
    LS = _lock_tmp(tmp_path, monkeypatch)
    LS.record_locked("", "[locked: no HTTP request context] Call unlock(...) first.", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert rf._looks_locked("unlock 未提供で STUCK。", since=99.0) is False


def test_a_refusal_from_a_real_client_still_counts(tmp_path, monkeypatch):
    """空 IP を無視するのであって、拒否そのものを無視するのではない。"""
    import relay.relay_fleet as rf
    LS = _lock_tmp(tmp_path, monkeypatch)
    LS.record_locked("203.0.113.7", "[locked client IP]", ts=100.0)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 101.0)
    assert rf._looks_locked("unlock 未提供で STUCK。", since=99.0) is True


def test_all_three_copies_of_the_prefix_agree():
    """同じリテラルが3箇所にある（security.py が書き、fleet と bridge が読む）。
    1つだけ変わると、読む側が黙って効かなくなる -- 静かに壊れる形。"""
    import io as _io

    import relay.relay_fleet as rf
    src = _io.open("tools/security.py", encoding="utf-8").read()
    bridge = _io.open("bridge/copilot_bridge.py", encoding="utf-8").read()
    assert rf.NO_CONTEXT_REFUSAL in src
    assert ('NO_CONTEXT_REFUSAL = "%s"' % rf.NO_CONTEXT_REFUSAL) in bridge


def test_a_conversation_id_resume_takes_the_socket(monkeypatch):
    """The other half of the rule above. Continuing a conversation needs its id and nothing
    else -- measured with a control arm, at one hour, across a fresh token -- so a follow-up
    that names one has no reason to pay for a tab."""
    seen = {}
    drv = _FakeDrv()

    def _route():
        def driver_for(self, n, **kw):
            seen.update(kw)
            return drv
        return type("R", (), {"driver_for": driver_for})()

    monkeypatch.setattr(rf, "_socket_route", _route)
    monkeypatch.setattr(rf, "_open_fresh",
                        lambda *a: pytest.fail("a conversation-id resume must not open a tab"))
    w = _worker()
    w.resume_conv = "4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    assert w.attach(object(), "agent-url") is True
    assert w.socket is True
    assert seen.get("conversation_id") == "4fe936fd-d902-497d-bc89-a2ad4ceb699c"


def test_a_fresh_worker_asks_for_no_conversation(monkeypatch):
    """Starting one stays the default: resuming the wrong conversation looks exactly like
    resuming the right one."""
    seen = {}
    drv = _FakeDrv()

    def _route():
        def driver_for(self, n, **kw):
            seen.update(kw)
            return drv
        return type("R", (), {"driver_for": driver_for})()

    monkeypatch.setattr(rf, "_socket_route", _route)
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: pytest.fail("no tab expected"))
    w = _worker()
    assert w.attach(object(), "agent-url") is True
    assert seen.get("conversation_id") == ""


def test_a_conversation_id_resume_with_no_socket_opens_the_agent_not_the_guid(monkeypatch):
    """`open_url` is resume_conv, so a conversation-id resume that could not get a socket
    would have handed a bare guid to goto(), failed all three attempts and lost the goal --
    and _agent_url kept the same value, so a mid-run fallback would open the same nonsense."""
    opened = {}

    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {"driver_for": lambda self, n, **kw: None})())

    def _fresh(ctx, url):
        opened["url"] = url
        return object()

    monkeypatch.setattr(rf, "_open_fresh", _fresh)
    w = _worker()
    w.resume_conv = "4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    w.attach(object(), "https://agent.example/chat")
    assert opened["url"] == "https://agent.example/chat"
    assert w.socket is False


def test_a_url_resume_still_opens_that_url(monkeypatch):
    """The fix above must not turn a working tab resume into a fresh conversation."""
    opened = {}
    monkeypatch.setattr(rf, "_open_fresh",
                        lambda ctx, url: opened.setdefault("url", url) or object())
    w = _worker()
    w.resume_conv = "https://example/chat/4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    w.attach(object(), "https://agent.example/chat")
    assert opened["url"] == "https://example/chat/4fe936fd-d902-497d-bc89-a2ad4ceb699c"


def test_the_lost_context_is_announced(monkeypatch, capsys):
    """A follow-up that quietly became a fresh conversation answers plausibly and wrongly."""
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {"driver_for": lambda self, n, **kw: None})())
    monkeypatch.setattr(rf, "_open_fresh", lambda ctx, url: object())
    w = _worker()
    w.resume_conv = "4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    w.attach(object(), "https://agent.example/chat")
    assert "will NOT see the earlier conversation" in capsys.readouterr().out


def test_a_mid_run_fallback_never_reopens_a_bare_conversation_id(monkeypatch):
    """_agent_url used to be assigned before the transport was decided, so a conversation-id
    resume left a bare guid in it -- and _fall_back_to_tab reopens exactly that."""
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {"driver_for": lambda self, n, **kw: None})())
    monkeypatch.setattr(rf, "_open_fresh", lambda ctx, url: object())
    w = _worker()
    w.resume_conv = "4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    w.attach(object(), "https://agent.example/chat")
    assert w._agent_url == "https://agent.example/chat"


def test_a_socket_resume_leaves_a_reopenable_url_behind(monkeypatch):
    """The socket worker may still fall back mid-run; what it falls back TO has to be a page."""
    drv = _FakeDrv()
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: type("R", (), {"driver_for": lambda self, n, **kw: drv})())
    monkeypatch.setattr(rf, "_open_fresh", lambda ctx, url: pytest.fail("no tab expected"))
    w = _worker()
    w.resume_conv = "4fe936fd-d902-497d-bc89-a2ad4ceb699c"
    w.attach(object(), "https://agent.example/chat")
    assert w.socket is True
    # Named exactly. "" also passes a not-a-guid check, and "" means "a fresh independent
    # chat" -- which is the silent context loss this change exists to stop.
    assert w._agent_url == "https://agent.example/chat"


def test_the_documented_default_matches_the_measured_one():
    """「MCP_FLEET_SOCKET が言わない限り OFF」と書かれていたが、実際の既定は ON。
    env の状態を人に説明するとき、最初に読まれるのがこの1行だった。"""
    import inspect
    from relay import relay_fleet as RF
    from relay import socket_route as SR
    doc = inspect.getsource(RF._socket_route)
    first = doc.split('"""')[1].splitlines()[0]
    assert "ON unless" in first, first
    assert SR.ENABLED is True, "この環境で既定が ON でないなら、上の1行も直すこと"


def test_a_route_built_with_no_log_path_cannot_reach_the_operators_record():
    """conftest のリポジトリ全体ガードが実際に効いていること。

    2026-08-21 に 13件の route_closed が本番の学習データに混ざった -- 理由は
    「FakeContext に ne 属性が無い」、エージェントURLは http://agent。閉じていない経路が
    13回閉じた記録になっていた。ガードはその後に入ったので、ここで固定しておく。
    log_path を渡し忘れた将来のテストが、また同じ行を書けてしまわないように。
    """
    r = SocketRoute(enabled=True, connect_fn=object())
    assert r.log_path != REAL_DEFAULT_LOG
    assert ".fleet" not in r.log_path.replace("\\", "/") or "live_records" in r.log_path


# ---- 事象の合流: 並列下の「連続」を数え直す --------------------------------------------------

def test_workers_failing_from_one_incident_vote_once(monkeypatch):
    """2026-08-25 の実測そのまま: w3 18:43:24 / w5 18:43:48 / w9 18:43:56。
    3ワーカー、32秒、1つの事象。それが31ターン運んだ経路を閉じた。"""
    r = _route()
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    monkeypatch.setattr(rf, "_LAST_ROUTE_FAULT", [0.0])
    clock = {"t": 1000.0}
    for offset in (0.0, 24.0, 32.0):
        clock["t"] = 1000.0 + offset
        rf.report_route_fault("w: dropped", now=lambda: clock["t"])
    assert r.consecutive == 1, "同一事象を %d 票として数えている" % r.consecutive
    assert r.open(), "瞬断1回で経路が閉じている"


def test_incidents_far_enough_apart_still_close_the_route(monkeypatch):
    """合流は遮断器を無効にするものではない。本当に塞がれたら閉じること。"""
    r = _route()
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    monkeypatch.setattr(rf, "_LAST_ROUTE_FAULT", [0.0])
    clock = {"t": 1000.0}
    for i in range(3):
        clock["t"] = 1000.0 + i * (rf.SOCKET_INCIDENT_WINDOW_S + 1)
        rf.report_route_fault("w: dropped", now=lambda: clock["t"])
    assert not r.open()
    assert "3 consecutive" in r.closed_reason


def test_a_suppressed_vote_is_not_a_suppressed_fallback(monkeypatch):
    """合流されるのは遮断器への1票だけ。ワーカーは退避するし、記録も残る。"""
    r = _route()
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    monkeypatch.setattr(rf, "_LAST_ROUTE_FAULT", [0.0])
    clock = {"t": 500.0}
    assert rf.report_route_fault("first", now=lambda: clock["t"]) is True
    clock["t"] += 1.0
    assert rf.report_route_fault("second", now=lambda: clock["t"]) is False


def test_the_window_lives_where_the_parallelism_is():
    """socket_route は凍結で、しかもここが正しい置き場所 --
    ワーカーを並列に走らせるモジュールが、並列であることを知っている唯一の側。"""
    import inspect

    import relay.socket_route as SR

    assert not hasattr(SR, "SOCKET_INCIDENT_WINDOW_S")
    assert "note_failure" in inspect.getsource(rf.report_route_fault)


# ---- 再送は「まだ届いていない」時だけ安い --------------------------------------------------
#
# 外部レビューの最重要指摘。`deadline exceeded` は「サーバに届いて実行中」を意味するのに、
# それを route 障害として同じ本文を再送していた。しかも予算を使い切るとカウンタが 0 に戻るので
# 上限が無い -- 「メールを送る」ゴールなら、20分ごとに送信が繰り返されうる。
# duplicate_risk() は同じ夜にタブ退避側へ入っていたのに、この経路は参照も記録もしていなかった。

DELIVERED_REASON = "ChatHubError: turn deadline exceeded before a completion frame"


def test_a_turn_that_may_have_landed_is_resent_at_most_once(monkeypatch):
    """『届いた』と分類される理由。ここは理由文字列で判る数少ない場合。"""
    from relay.transport_policy import delivery_status
    assert delivery_status(DELIVERED_REASON) in ("delivered", "unknown")

    _retry_route(monkeypatch, drv=_FakeDrv())
    monkeypatch.setattr(rf, "_open_fresh", lambda *a: object())
    monkeypatch.setattr(rf, "CopilotWebDriver", lambda page: _FakeDrv())
    w = _worker()
    w._context, w._agent_url = object(), "u"
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"

    w.drv.answers = 0
    w.drv._partial_text = "途中まで届いていた"      # このターンはモデルに届いている
    w.drv.partial_text = lambda: w.drv._partial_text
    assert w._retry_socket(DELIVERED_REASON) is True
    assert w._socket_reconnects_total == 1
    w.drv.partial_text = lambda: "また届いた"
    assert w._retry_socket(DELIVERED_REASON) is False, "届いたターンを繰り返し再送している"


def test_a_turn_that_never_landed_gets_the_full_budget(monkeypatch):
    _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    for i in range(rf.SOCKET_RECONNECTS_PER_GOAL):
        assert w._retry_socket(DROPPED) is True, "attempt %d" % (i + 1)
    assert w._retry_socket(DROPPED) is False, "無制限に再接続している"


def test_the_total_is_never_reset_by_spending_a_budget(monkeypatch):
    """予算ごとのカウンタは 0 に戻る（それが投票の単位）。ゴール単位の総数は戻らない。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w._retry_socket(DROPPED)
    w._socket_retries = 0                      # 予算を使い切って報告した直後の状態
    w._retry_socket(DROPPED)
    assert w._socket_reconnects_total == 2


def test_the_record_says_whether_the_resend_could_repeat_an_act(monkeypatch, tmp_path):
    r = _route_logging(tmp_path)
    r.open = lambda: True
    r.driver_for = lambda n, **kw: _FakeDrv()
    monkeypatch.setattr(rf, "_socket_route", lambda: r)
    w = rf.RelayWorker("メールを送って", "w0")
    w.socket, w.drv, w.status = True, _FakeDrv(), "waiting"
    w.drv.partial_text = lambda: "半分だけ返ってきた"
    w._retry_socket(DELIVERED_REASON)
    row = next(x for x in _lines(tmp_path) if x["event"] == "socket_retry")
    assert row["landed"] is True and row["saw_output"] is True
    assert row["delivery"] in ("delivered", "unknown")
    assert row["cap"] == rf.SOCKET_RECONNECTS_IF_DELIVERED


def test_output_already_received_is_what_marks_a_turn_as_landed(monkeypatch):
    """理由文字列はほぼ全部 unknown を返す。トークンを受け取っていたかどうかが、
    こちらの手元にある唯一の確かな証拠。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    quiet = _worker()
    quiet.socket, quiet.drv, quiet.status = True, _FakeDrv(), "waiting"
    assert quiet._retry_socket(DROPPED) is True
    assert quiet._retry_socket(DROPPED) is True, "何も受信していないのに上限1にしている"

    spoke = _worker()
    spoke.socket, spoke.drv, spoke.status = True, _FakeDrv(), "waiting"
    spoke.drv.partial_text = lambda: "モデルは既に喋っていた"
    assert spoke._retry_socket(DROPPED) is True
    spoke.drv.partial_text = lambda: "また喋っている"
    assert spoke._retry_socket(DROPPED) is False


def test_a_completed_answer_also_counts_as_landed(monkeypatch):
    """partial が無くても、完了回答が1つでもあればそのターンは届いている。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(answers=1), "waiting"
    assert w._retry_socket(DROPPED) is True
    assert w._retry_socket(DROPPED) is False


def test_what_was_learned_about_a_turn_outlives_the_connection(monkeypatch):
    """再接続すると新しいドライバは何も見ていない。そこで判定し直すと、
    モデルに届いたと分かっているターンが『届いていない』に戻り、
    1回だったはずの上限が満額に化ける -- 1ターンが7回送られうる。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(answers=1), "waiting"
    assert w._retry_socket(DROPPED) is True
    assert w.drv._answers().count() == 0, "前提: 新しい接続は何も受信していない"
    assert w._retry_socket(DROPPED) is False, "着地の事実が接続と一緒に失われている"


def test_a_later_turn_is_judged_on_its_own_evidence(monkeypatch):
    """粘るのは『そのターンについて』であって、ワーカーに焼き付けるものではない。"""
    _retry_route(monkeypatch, drv=_FakeDrv())
    w = _worker()
    w.socket, w.drv, w.status = True, _FakeDrv(answers=1), "waiting"
    w._retry_socket(DROPPED)
    w.turn += 1                                   # 次のターンへ進んだ
    assert w._retry_socket(DROPPED) is True
