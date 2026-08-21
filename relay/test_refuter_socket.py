"""レビューは socket で走らせる。走らないレビューは、短いレビューではなく無いレビュー。

RAM ゲートに阻まれた refuter は RAM_SKIP になり、候補は**未レビューのまま受理**される。
今日この機体で ram_room_for_tab() は実際に False だった。socket はこの門を通らない。
"""
import types

import pytest

from relay import refuter as RF


class _FakeDrv:
    def __init__(self):
        self.failed = ""
        self.closed = False
        self.sent = []
        self._count_before = 0

    def send(self, text, **_kw):
        self.sent.append(text)

    def _answers(self):
        return types.SimpleNamespace(count=lambda: len(self.sent))

    def read_last_response(self):
        return ""

    def close(self):
        self.closed = True


class _Route:
    def __init__(self, driver=None, open_=True):
        self.driver, self._open = driver, open_
        self.asked, self.failures, self.records = [], [], []

    def open(self):
        return self._open

    def needs_refresh(self, url=None):
        return False

    def refresh(self, ctx, url):
        return True

    def driver_for(self, name, agent_url=None, model="", turn_timeout_s=600.0,
                   frame_timeout_s=90.0):
        self.asked.append({"agent_url": agent_url, "turn_timeout_s": turn_timeout_s})
        return self.driver

    def note_failure(self, why):
        self.failures.append(why)

    def record(self, event, **f):
        self.records.append((event, f))


def _session(route, monkeypatch, **kw):
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    kw.setdefault("timeout_s", 600)
    return RF.RefuterSession(object(), "https://agent/impl", "目標", "最終回答", **kw)


def test_a_review_takes_a_socket_and_opens_no_page(monkeypatch):
    drv = _FakeDrv()
    s = _session(_Route(driver=drv), monkeypatch)
    assert s._try_socket() is True
    assert s.socket is True and s.page is None and s.drv is drv
    assert s._pending_open is False, "RAM ゲートを通ってはならない"
    assert s.drv.sent and "目標" in s.drv.sent[0]


def test_it_reviews_on_the_agent_it_was_pointed_at(monkeypatch):
    route = _Route(driver=_FakeDrv())
    s = _session(route, monkeypatch, timeout_s=900)
    s._try_socket()
    assert route.asked[0]["agent_url"] == "https://agent/impl"
    assert route.asked[0]["turn_timeout_s"] == 450.0, "socket が予算を独り占めしている"


def test_a_lens_reaches_the_socket_prompt(monkeypatch):
    """パネルの観点が落ちると、3人が同じレビューをして多数決が意味を失う。"""
    drv = _FakeDrv()
    s = _session(_Route(driver=drv), monkeypatch, lens="security")
    s._try_socket()
    expected = RF.build_refuter_prompt("目標", "最終回答", lens="security")
    assert drv.sent == [expected]


def test_a_route_that_offers_nothing_falls_through_to_a_page(monkeypatch):
    s = _session(_Route(driver=None), monkeypatch)
    assert s._try_socket() is False
    assert s.socket is False and s._pending_open is True


def test_a_dead_socket_becomes_a_page_review_and_is_recorded(monkeypatch):
    drv = _FakeDrv()
    route = _Route(driver=drv)
    s = _session(route, monkeypatch)
    s._try_socket()
    drv.failed = "ChatHubError: the socket went silent"
    assert s.poll() is None
    assert s.socket is False and s.drv is None and s._pending_open is True
    assert route.failures and "went silent" in route.failures[0]
    assert any(e == "fallback" for e, _ in route.records)


def test_the_socket_is_tried_exactly_once_when_it_is_not_available(monkeypatch):
    """捕獲は実タブ1枚と実ターン1回。毎 poll 試すと、避けようとしたタブより高くつく。"""
    route = _Route(driver=None)
    s = _session(route, monkeypatch)
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "ram_room_for_tab", lambda: False)
    s.start()
    for _ in range(3):
        s.poll()
    assert len(route.asked) == 1, "0回なら socket を一度も試していない"
    assert s._socket_tried is True


def test_a_working_socket_means_the_page_is_never_opened(monkeypatch):
    """『socket を試す』を消しても、テストが件数の上限しか見ていないと気づけない。
    実際に見るべきは『タブが開かれないこと』。"""
    route = _Route(driver=_FakeDrv())
    s = _session(route, monkeypatch)
    monkeypatch.setattr(s, "_do_open",
                        lambda: pytest.fail("socket が取れているのにページを開いた"))
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "ram_room_for_tab", lambda: True)
    s.start()
    s.poll()
    assert s.socket is True and len(route.asked) == 1


def test_the_page_path_carries_the_lens_too(monkeypatch):
    """観点が落ちると、3人のパネルが同じレビューを3回して多数決が意味を失う。
    socket 側だけ試験していて、ページ側は無試験だった -- 変異が素通りして分かった。"""
    sent = []

    class _Page:
        def goto(self, *a, **k):
            pass

        def wait_for_timeout(self, _ms):
            pass

        def locator(self, _sel):
            return types.SimpleNamespace(count=lambda: 1)

        def close(self):
            pass

    class _Drv2(_FakeDrv):
        def send(self, text, **_kw):
            sent.append(text)

    class _Ctx:
        def new_page(self):
            return _Page()

    import relay.copilot_autopilot_relay as CAR
    monkeypatch.setattr(CAR, "CopilotWebDriver", lambda page: _Drv2())
    s = RF.RefuterSession(_Ctx(), "https://agent/impl", "目標", "最終回答", lens="security")
    s._do_open()
    assert sent == [RF.build_refuter_prompt("目標", "最終回答", lens="security")]


def test_the_offline_probe_is_not_run_against_a_socket(monkeypatch):
    """_page_network_available はページを見る。socket にページは無い。
    ここを守らないと、健全な socket レビューが毎 poll で『オフライン』扱いになる。"""
    drv = _FakeDrv()
    s = _session(_Route(driver=drv), monkeypatch)
    s._try_socket()
    import relay.copilot_autopilot_relay as CAR
    monkeypatch.setattr(CAR, "_page_network_available",
                        lambda page: pytest.fail("socket にページの健全性を尋ねてはいけない"))
    assert s.poll() is None


def test_closing_a_socket_review_closes_its_conversation(monkeypatch):
    drv = _FakeDrv()
    s = _session(_Route(driver=drv), monkeypatch)
    s._try_socket()
    s.close()
    assert drv.closed is True and s.socket is False


def test_the_socket_never_spends_the_whole_budget(monkeypatch):
    """実測 2026-08-21: socket レビューが 400秒の期限を使い切って落ち、そこからページが
    約210秒で完了した -- ページなら約210秒の作業に合計853秒。主ワーカーの1ターンと違い、
    長時間の副エージェントでは失敗した socket が期限まるごとを食う。"""
    for budget, expected in ((600, 300.0), (400, 200.0), (100, 120.0), (0, 120.0)):
        assert RF._socket_share(budget) == expected


def test_a_tiny_budget_still_gives_the_socket_a_usable_window():
    """半分にした結果 socket が何も終えられない大きさになると、
    毎回『試して落ちる』ぶんだけ遅くなる。下限を置く理由。"""
    assert RF._socket_share(60) == 120.0
    assert RF._socket_share(None) == 120.0
