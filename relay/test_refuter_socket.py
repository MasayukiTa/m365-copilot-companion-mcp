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


class _OkGate:
    def reason(self):
        return ""


@pytest.fixture(autouse=True)
def _mcp_ready(monkeypatch):
    """The MCP creation gate probes 127.0.0.1:8000; a unit test must not depend on it."""
    from relay import mcp_ready
    monkeypatch.setattr(mcp_ready, "gate", lambda: _OkGate())


class _Route:
    enabled = True
    default_agent_url = "https://agent/impl"

    def __init__(self, driver=None, open_=True):
        self.driver, self._open = driver, open_
        self.asked, self.failures, self.records = [], [], []

    def open(self):
        return self._open

    def ready(self, url=None):
        return self._open and self.driver is not None

    def template_for(self, url=None):
        return object() if self.driver is not None else None

    def needs_refresh(self, url=None):
        return False

    def refresh(self, ctx, url):
        raise AssertionError("a synchronous capture on the sweep thread")

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
    assert route.asked[0]["turn_timeout_s"] == 900.0, "ページへの退避は無いので予算は全部使える"


def test_a_lens_reaches_the_socket_prompt(monkeypatch):
    """パネルの観点が落ちると、3人が同じレビューをして多数決が意味を失う。"""
    drv = _FakeDrv()
    s = _session(_Route(driver=drv), monkeypatch, lens="security")
    s._try_socket()
    expected = RF.build_refuter_prompt("目標", "最終回答", lens="security")
    assert drv.sent == [expected]


def test_a_route_that_offers_nothing_holds_the_review(monkeypatch):
    s = _session(_Route(driver=None), monkeypatch)
    assert s._try_socket() is False
    assert s.socket is False and s._pending_open is True
    assert s.hold_reason, "待つ理由が言われていない"
    assert s._done is None, "保留が判定になった"


def test_a_dead_socket_is_recorded_and_the_review_asks_for_a_socket_again(monkeypatch):
    """張り直しを使い切ったあとも、ページには退避しない(禁止)。記録を残して保留に戻り、
    次の poll でまた socket を取りに行く。経路の故障はレビューの故障ではない。"""
    from relay.relay_fleet import DEFAULT_SOCKET_RETRIES

    drv = _FakeDrv()
    route = _Route(driver=drv)
    s = _session(route, monkeypatch)
    s._try_socket()
    s._socket_retries = DEFAULT_SOCKET_RETRIES          # 張り直しは使い切った
    drv.failed = "ChatHubError: the socket went silent"
    assert s.poll() is None
    assert s.socket is False and s.drv is None
    assert s._pending_open is True
    assert route.failures and "went silent" in route.failures[0]
    assert any(e == "fallback" for e, _ in route.records)


def test_a_dropped_connection_reconnects_before_it_gives_up_the_socket(monkeypatch):
    """伝送の故障で即pageに退避していた。ワーカー側だけ直して
    ここを掃引し忘れたため、2026-08-25 20:28 に refuter だけが同じ切断で
    ページを開いていた -- 隣のワーカーは同じ切断を張り直しで越えていた。"""
    drv = _FakeDrv()
    route = _Route(driver=drv)
    s = _session(route, monkeypatch)
    s._try_socket()
    drv.failed = "ChatHubError: the socket went silent"
    assert s.poll() is None
    assert route.failures == [], "張り直しを経路の故障として数えている"
    assert s._socket_tried is False, "次の poll で socket を取り直せない"
    assert s._socket_retries == 1

def test_a_held_review_never_ends_unclear_and_never_opens_a_page(monkeypatch):
    """実害(2026-10-15): socket が一度取れないと page を開こうとして CopilotTabForbidden ->
    UNCLEAR になり、独立レビューが一度も走らなかった。今は保留し、取れた時点で走る。"""
    route = _Route(driver=None)
    s = _session(route, monkeypatch)
    monkeypatch.setattr(s, "_do_open", lambda: pytest.fail("ページを開こうとした"))
    s.start()
    for _ in range(5):
        s._next_try_at = 0.0
        assert s.poll() is None
    assert s._done is None and s.hold_reason
    drv = _FakeDrv()
    route.driver = drv                     # token / template が揃った
    s._next_try_at = 0.0
    assert s.poll() is None
    assert s.socket is True and drv.sent, "取れた時点でレビューが送られていない"


def test_a_held_review_does_not_burn_its_budget_waiting(monkeypatch):
    """待っている時間はレビュアーの遅延ではない。待ちで timeout_s を越えて UNCLEAR にしない。"""
    s = _session(_Route(driver=None), monkeypatch, timeout_s=10)
    s.start()
    s._t_send -= 10_000
    s._next_try_at = 0.0
    assert s.poll() is None and s._done is None


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


def test_the_page_path_is_refused_not_opened(monkeypatch):
    """The page path (and its lens handling) was removed with the tab: a refuter that cannot
    get a socket fails with a harness reason instead of opening a page."""
    class _Ctx:
        def new_page(self):
            raise AssertionError("opened a page")

    s = RF.RefuterSession(_Ctx(), "https://agent/impl", "目標", "最終回答", lens="security")
    s._do_open()
    assert s._done is not None and s._done[0] == "UNCLEAR"
    assert s._done[1].startswith("harness: ")


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
