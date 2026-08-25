"""空のレポートには必ず理由が付く。理由の無い空は、調べようがない障害になる。

実測 (2026-08-21): 実機で research を1本通したところ、承認は21秒で自動送信され、
93秒で計画ブロックが1529文字まで育ち、103秒で**空**を返して終了した。
タイムアウトは1500秒。つまり例外が握り潰され、「クラッシュした research」と
「何も見つからなかった research」が外から完全に同じ見た目になっていた。
"""
import types

import pytest

from relay import agent_profiles as AP


def _session():
    s = AP.ResearchSession.__new__(AP.ResearchSession)
    s._done = None
    s.error = ""
    s.socket = False
    s._socket_tried = True
    s.tx_dir = None
    s.parent_key = ""
    s._report_full = ""
    s.page = None
    s.drv = None
    return s


def test_an_empty_report_always_carries_a_reason():
    s = _session()
    s._fail("timeout: 1500s without a finished report")
    assert s._done == ""
    assert "timeout" in s.error


def test_a_swallowed_exception_is_no_longer_swallowed():
    """poll() の except 節がここに来る。理由が消えると、1500秒の予算に対して
    103秒で空を返した事実が『調査が何も見つけなかった』に化ける。"""
    s = _session()
    s._t_send = 0.0
    s.timeout_s = 1500
    s._pending_open = False

    class _Boom:
        def _answers(self):
            raise RuntimeError("Target page, context or browser has been closed")

    s.drv = _Boom()
    import time as _t
    s._t_send = _t.time()
    out = s.poll()
    assert out == ""
    assert "RuntimeError" in s.error
    assert "closed" in s.error


def test_the_reason_distinguishes_the_ways_of_failing():
    """RAM が無かった / 時間切れ / 落ちた は運用上まったく別の話で、
    同じ '' で表現されていると、どれを直せばよいか決められない。"""
    reasons = []
    for r in ("no tab within 600s: the box had no RAM for a side page",
              "timeout: 1500s without a finished report",
              "TargetClosedError: page closed"):
        s = _session()
        s._fail(r)
        reasons.append(s.error)
    assert len(set(reasons)) == 3


def test_a_session_that_succeeded_carries_no_reason():
    s = _session()
    s._finish("本文" * 100)
    assert s.error == ""
    assert s._done.startswith("本文")


# ---- 2回目以降の確認 ---------------------------------------------------------------------
#
# Researcher は 1 回で終わるとは限らない。承認が一度きりだと、2 回目の質問は無視され、
# セッションは 1500 秒の予算を丸ごと待ってから空を返す -- 25 分かけて何も出さない経路。

class _ScriptedDrv:
    """送信するたびに次のブロックへ進む台本ドライバ。"""

    def __init__(self, blocks):
        self.blocks = list(blocks)
        self.sent = []
        self.i = 0
        self.count = 1

    def _answers(self):
        return types.SimpleNamespace(count=lambda: self.count)

    def read_last_response(self):
        return self.blocks[min(self.i, len(self.blocks) - 1)]

    def send(self, text):
        self.sent.append(text)
        self.i += 1
        self.count += 1

    def close(self):
        pass


CLARIFY_1 = "To make sure I cover what you need: A) 企業内検索 B) 一般 どちらですか。"
CLARIFY_2 = "To make sure I cover what you need: 期間は 2025-2026 に限定しますか。"


def _live_session(blocks, max_approvals):
    s = AP.ResearchSession.__new__(AP.ResearchSession)
    s.drv = _ScriptedDrv(blocks)
    s._count_before = 0
    s._t_send = __import__("time").time()
    s.timeout_s = 10_000
    s.dwell_s = 2.0
    s.approval = AP.DEFAULT_APPROVAL
    s.max_approvals = max_approvals
    s._approvals = 0
    s._approved = False
    s._last = None
    s._stable_since = None
    s._settle_state = __import__("relay.settle", fromlist=["x"]).SettleState()
    s._done = None
    s._pending_open = False
    # These sessions stand in for the TAB path: the settle/approval rules under test are about
    # DOM text, and a socket turn ends by protocol instead.
    s.socket = False
    s._socket_tried = True
    s.upload_path = ""
    s._report_full = ""
    s.error = ""
    s.page = None
    s.tx_dir = None
    s.parent_key = ""
    return s


def test_a_second_scoping_question_is_answered_too():
    """一度きりの承認では、ここで沈黙して 25 分後に空が返る。"""
    s = _live_session([CLARIFY_1, CLARIFY_2, "本文" * 800], max_approvals=3)
    s.poll()
    assert s.drv.sent == [AP.DEFAULT_APPROVAL]
    s.poll()
    assert s.drv.sent == [AP.DEFAULT_APPROVAL, AP.DEFAULT_APPROVAL], \
        "2回目の確認に答えていない -- 予算を使い切って空で終わる経路"


def test_being_asked_forever_does_not_consume_the_whole_budget():
    """聞かれ続けるエージェントに無限に答えると、調査の予算が質疑応答で消える。"""
    s = _live_session([CLARIFY_1] * 10, max_approvals=3)
    for _ in range(10):
        s.poll()
    assert len(s.drv.sent) == 3


def test_a_session_configured_without_approval_never_sends_one():
    s = _live_session([CLARIFY_1, CLARIFY_1], max_approvals=3)
    s.approval = ""
    s.poll()
    s.poll()
    assert s.drv.sent == []


# ---- ブロッキング側 (_wait_research_done) も同じ穴が空いていた -------------------------------
#
# ask_agent は最初のブロックにだけ承認を返し、その後は _wait_research_done に入る。
# そちらには承認処理が存在しなかったので、2回目の質問は deadline まで無視されていた。
# 「故障クラスは全呼び出し元を掃引しろ」-- 片方だけ直すと、直っていない方で再発する。

class _Profile:
    name = "researcher"
    end_timeout_s = 30
    appear_timeout_s = 5
    dwell_s = 0.1


def test_the_blocking_loop_answers_a_second_question_too(monkeypatch):
    monkeypatch.setattr(AP, "stop_check", lambda *a, **k: "")
    drv = _ScriptedDrv([CLARIFY_1, CLARIFY_2, "本文" * 800])
    drv._count_before = 0
    ok = AP._wait_research_done(drv, _Profile(), approval=AP.DEFAULT_APPROVAL,
                                approvals_left=3)
    assert ok is True
    assert drv.sent == [AP.DEFAULT_APPROVAL, AP.DEFAULT_APPROVAL]


def test_the_blocking_loop_without_a_budget_answers_nothing(monkeypatch):
    """呼び出し側が承認を渡していないときに勝手に答え始めてはいけない。"""
    monkeypatch.setattr(AP, "stop_check", lambda *a, **k: "")
    drv = _ScriptedDrv([CLARIFY_1, "本文" * 800])
    drv._count_before = 0
    AP._wait_research_done(drv, _Profile(), approval=AP.DEFAULT_APPROVAL, approvals_left=0)
    assert drv.sent == []


def test_the_blocking_loop_stops_being_asked_forever(monkeypatch):
    monkeypatch.setattr(AP, "stop_check", lambda *a, **k: "")
    drv = _ScriptedDrv([CLARIFY_1] * 12)
    drv._count_before = 0
    AP._wait_research_done(drv, _Profile(), approval=AP.DEFAULT_APPROVAL, approvals_left=2)
    assert len(drv.sent) == 2


def test_ask_agent_hands_its_remaining_budget_to_the_wait_loop(monkeypatch):
    """承認1回分は ask_agent 自身が使うので、残りを渡さないと 2 回目に答えられない。
    渡し忘れても両者のテストは緑のままになる -- 配線そのものを見張る。"""
    got = {}

    class _Drv:
        def __init__(self, page):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

        def wait_for_idle(self, **kw):
            return True

        def read_last_response(self):
            return CLARIFY_1

    def _spy(drv, profile, approval="", approvals_left=0):
        got["approval"] = approval
        got["left"] = approvals_left
        return True

    monkeypatch.setattr(AP, "CopilotWebDriver", _Drv)
    monkeypatch.setattr(AP, "open_agent", lambda page, profile: True)
    monkeypatch.setattr(AP, "set_model", lambda page, profile, name: True)
    monkeypatch.setattr(AP, "current_model", lambda page, profile: "Claude")
    monkeypatch.setattr(AP, "stop_check", lambda *a, **k: "")
    monkeypatch.setattr(AP, "_wait_research_done", _spy)

    out = AP.ask_agent(object(), "調べて", approval=AP.DEFAULT_APPROVAL)
    assert out["ok"] is True
    assert got["approval"] == AP.DEFAULT_APPROVAL
    assert got["left"] == AP.MAX_APPROVALS - 1, "ask_agent が使った1回分が引かれていない"


# ---- 完了判定は Stop ボタンで決める（実測 2026-08-21、2本の完走観測より） --------------------
#
#   完了マーカー : 一度も出ない（11,507文字の完成レポートが2分静止しても不在）
#   文字数       : 30-45秒で1000字を超える（レポート完成の約10分前）
#   静止時間     : 実行中に169秒静止した。「1000字以上かつ24秒静止」は実行中に16回成立し、
#                  最も早いものは6,060文字＝最終の52%だった
#   本文の長さ   : 6060 → 929 → 1475 → 11507 と縮む。単調性が無い
#   Stop ボタン  : 実行中ずっと True、673秒で False、その時点で本文は最終形
#
# つまりテキストから決めることは原理的にできず、DOM の状態だけが分離できる。

class _GenDrv(_ScriptedDrv):
    """Stop ボタンの状態を持つ台本ドライバ。"""

    def __init__(self, blocks, generating):
        super().__init__(blocks)
        self.generating = generating

    def _is_generating(self):
        return self.generating


def test_a_settled_block_is_not_accepted_while_the_agent_is_still_working():
    """実測 t=259s の再現: 6,060文字が106秒静止していたが、まだ調査中だった。"""
    s = _live_session(["本文" * 3000], max_approvals=0)
    s.drv = _GenDrv(["本文" * 3000], generating=True)
    s.drv._count_before = 0
    s._count_before = 0
    s._last = "本文" * 3000
    s._stable_since = 0.0                      # 遥か昔から静止している扱い
    assert s.poll() is None
    assert s._done is None
    assert s._stable_since is None, "実行中に集めた静止は証拠にならないので捨てる"


def test_the_same_block_is_accepted_once_the_agent_has_stopped():
    body = "本文" * 3000
    s = _live_session([body], max_approvals=0)
    s.drv = _GenDrv([body], generating=False)
    s.drv._count_before = 0
    s._count_before = 0
    for _ in range(6):                          # dwell を跨ぐまで回す
        out = s.poll()
        if out is not None:
            break
        import time as _t
        _t.sleep(1.2)
    assert s._done and s._done.startswith("本文")


def test_a_driver_without_the_probe_behaves_as_before():
    """socket ドライバや古いスタブには Stop ボタンが無い。
    無いことを『実行中』と解釈すると、そちらの経路が永久に受理しなくなる。"""
    assert AP._still_generating(object()) is False


def test_a_probe_that_raises_does_not_freeze_a_finished_research():
    class _Broken:
        def _is_generating(self):
            raise RuntimeError("page closed")

    assert AP._still_generating(_Broken()) is False


def test_the_blocking_loop_waits_for_the_stop_button_too(monkeypatch):
    """同じ規則を両方の経路に入れる -- 片方だけだと、直っていない方で再発する。"""
    monkeypatch.setattr(AP, "stop_check", lambda *a, **k: "")
    drv = _GenDrv(["本文" * 3000], generating=True)
    drv._count_before = 0

    class _Profile2:
        name = "researcher"
        end_timeout_s = 3
        appear_timeout_s = 2
        dwell_s = 0.1

    assert AP._wait_research_done(drv, _Profile2()) is False   # 実行中は受理しない
    drv.generating = False
    drv._count_before = 0
    assert AP._wait_research_done(drv, _Profile2()) is True


# ---- A: 副エージェントを socket に載せる（実測 2026-08-21） -----------------------------------
#
# 実測: Researcher の1ターンが socket で 255 秒（タブ経路の同等調査は 673〜809 秒）、
# 出典付き 5,906 文字、進捗29件、モデル自己申告は Claude、そしてタブは一度も開いていない。
# バックエンドが完了フレームを送るので、Stop ボタンも静止判定も要らない。

class _FakeDrv:
    """ドライバの代役。socket ドライバと同じ約束だけを持つ。"""

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
    def __init__(self, driver=None, open_=True, refreshed=True):
        self.driver, self._open, self._refreshed = driver, open_, refreshed
        self.asked = []
        self.failures = []
        self.records = []

    def open(self):
        return self._open

    def needs_refresh(self, url=None):
        return not self._refreshed

    def refresh(self, ctx, url):
        return self._refreshed

    def driver_for(self, name, agent_url=None, model="", turn_timeout_s=600.0,
                   frame_timeout_s=90.0):
        self.asked.append({"agent_url": agent_url, "model": model,
                           "turn_timeout_s": turn_timeout_s})
        return self.driver

    def note_failure(self, why):
        self.failures.append(why)

    def record(self, event, **f):
        self.records.append((event, f))


def _session_for_socket(route, monkeypatch, **kw):
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    s = AP.ResearchSession(object(), "調べて", **kw)
    return s


def test_a_deep_dive_takes_a_socket_and_opens_no_tab(monkeypatch):
    drv = _FakeDrv()
    route = _Route(driver=drv)
    monkeypatch.setattr(AP, "open_agent",
                        lambda *a, **k: pytest.fail("a socket deep-dive must not open a tab"))
    s = _session_for_socket(route, monkeypatch)
    assert s._try_socket() is True
    assert s.socket is True and s.page is None and s.drv is drv
    assert s._pending_open is False, "RAM ゲートを通ってはならない -- socket はタブではない"


def test_it_asks_for_its_own_agent_and_its_own_model(monkeypatch):
    """Researcher は実装エージェントとは別物。共有テンプレートで送ると、
    研究のターンが黙って別のエージェントに届き、そのエージェントは普通に答える。"""
    route = _Route(driver=_FakeDrv())
    s = _session_for_socket(route, monkeypatch, model_name="Claude", timeout_s=1234)
    s._try_socket()
    asked = route.asked[0]
    assert asked["agent_url"] == AP.RESEARCHER.url
    assert asked["model"] == "Claude"
    # socket にはセッション予算の半分だけ渡す。全額渡すと、失敗したときページの取り分が
    # ゼロになる -- 実測でそれが起きて、210秒で済む作業に 853秒かかった。
    assert asked["turn_timeout_s"] == 617.0, "socket が予算を独り占めしている"


def test_the_analyst_never_asks_for_a_socket(monkeypatch):
    """アップロードは <input type=file> に入る。socket に置き場は無い。"""
    route = _Route(driver=_FakeDrv())
    s = _session_for_socket(route, monkeypatch, upload_path=r"C:\d.csv")
    assert s._try_socket() is False
    assert route.asked == [], "そもそも尋ねてもいけない"
    assert s.socket is False


def test_a_route_that_offers_nothing_falls_through_to_the_tab(monkeypatch):
    s = _session_for_socket(_Route(driver=None), monkeypatch)
    assert s._try_socket() is False
    assert s.socket is False and s._pending_open is True


def test_a_closed_route_is_not_asked(monkeypatch):
    route = _Route(driver=_FakeDrv(), open_=False)
    s = _session_for_socket(route, monkeypatch)
    assert s._try_socket() is False
    assert route.asked == []


def test_the_socket_is_tried_once_and_not_on_every_poll(monkeypatch):
    """捕獲は実タブ1枚と実ターン1回。毎 poll 試すと、避けようとしたタブより高くつく。"""
    route = _Route(driver=None)
    s = _session_for_socket(route, monkeypatch)
    monkeypatch.setattr(AP, "ram_room_for_tab", lambda: False, raising=False)
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "ram_room_for_tab", lambda: False)
    s.start()
    s.poll()
    s.poll()
    s.poll()
    assert s._socket_tried is True
    assert len(route.asked) <= 1


def test_a_failed_socket_deep_dive_becomes_a_tab_deep_dive(monkeypatch):
    """予算を使い切ったあとは、これまでどおりtab に退避して記録も残る。

    経路の故障は深掘りの故障ではない、という元の不変条件はここで守られる。
    変わったのは、そこに至る前に接続を張り直すようになったことだけ。
    """
    from relay.relay_fleet import DEFAULT_SOCKET_RETRIES

    drv = _FakeDrv()
    route = _Route(driver=drv)
    s = _session_for_socket(route, monkeypatch)
    s._try_socket()
    s._socket_retries = DEFAULT_SOCKET_RETRIES          # 張り直しは使い切った
    drv.failed = "ChatHubError: the socket went silent"
    assert s.poll() is None
    assert s.socket is False and s.drv is None
    assert s._pending_open is True
    assert route.failures and "went silent" in route.failures[0]
    assert any(e == "fallback" for e, _ in route.records)


def test_a_dropped_connection_reconnects_before_it_gives_up_the_socket(monkeypatch):
    """伝送の故障で即tabに退避していた。ワーカー側だけ直して
    ここを掃引し忘れたため、2026-08-25 20:28 に refuter だけが同じ切断で
    ページを開いていた -- 隣のワーカーは同じ切断を張り直しで越えていた。"""
    drv = _FakeDrv()
    route = _Route(driver=drv)
    s = _session_for_socket(route, monkeypatch)
    s._try_socket()
    drv.failed = "ChatHubError: the socket went silent"
    assert s.poll() is None
    assert route.failures == [], "張り直しを経路の故障として数えている"
    assert s._socket_tried is False, "次の poll で socket を取り直せない"
    assert s._socket_retries == 1

def test_closing_a_socket_deep_dive_closes_its_conversation(monkeypatch):
    drv = _FakeDrv()
    s = _session_for_socket(_Route(driver=drv), monkeypatch)
    s._try_socket()
    s.close()
    assert drv.closed is True
    assert s.socket is False and s.drv is None
