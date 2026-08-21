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
                            "note_failure": lambda self, why: noted.append(why),
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
