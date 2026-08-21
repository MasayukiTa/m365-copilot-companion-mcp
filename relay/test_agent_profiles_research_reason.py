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
