"""socket 経路が「速くなるだけ、壊れても仕事は落ちない」を守っているか。

この経路の前提は『Microsoft がいつ塞いでもおかしくない』こと。だからここで確かめるのは
速さではなく、塞がれた時に何が起きるか -- タブに戻る、戻り続けない、そして仕事は進む。
"""
import pytest

from relay import relay_fleet as rf
from relay.socket_route import SocketRoute


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

def test_the_route_is_off_unless_it_is_switched_on():
    """まだ長時間の実運用を通していない。既定は観測済みのことについてだけ主張する。"""
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

    r._token = _token(seconds=100)      # 期限が迫った
    assert r.needs_refresh() is True


def test_a_running_conversation_sees_a_refreshed_token():
    """トークンではなく供給者を渡している。目標の途中で更新が起きても、
    走っている会話がそれを掴める。"""
    r = _route(capture_fn=lambda *a: (_token(), _Tpl()))
    r.refresh(object(), "u")
    drv = r.driver_for("w0")
    assert drv is not None
    first = drv.conv._token_supplier()
    r._token = _token(seconds=7200)
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
                        lambda: type("R", (), {"driver_for": lambda self, n: drv})())
    monkeypatch.setattr(rf, "_open_fresh",
                        lambda *a: pytest.fail("a socket worker must not open a tab"))
    w = _worker()
    assert w.attach(object(), "agent-url") is True
    assert w.socket and w.drv is drv and w.page is None
    assert w.status == "ready"


def test_a_resuming_worker_is_never_given_a_socket(monkeypatch):
    """resume は『あの URL をもう一度開く』という意味で、socket に開く URL は無い。"""
    monkeypatch.setattr(rf, "_socket_route",
                        lambda: pytest.fail("resume must not even ask for a socket"))
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
                            "driver_for": lambda self, n: None,
                            "note_failure": lambda self, why: noted.append(why)})())
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
