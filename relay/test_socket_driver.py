"""socket ドライバがタブ用ドライバと同じ約束を守っているかを固定する。

このドライバの価値は「艦隊のターンループがどちらを掴んでいるか区別できないこと」に尽きる。
区別できた瞬間、settle 規則・stale 捕捉ガード・部分表示という実障害から書かれた規律を
二重に書き直すことになる。だからここで見るのは形であって中身ではない。
"""
import threading
import time

import pytest

from relay import socket_driver as SD
from relay.copilot_autopilot_relay import GenerationInProgress


class _FakeConv:
    """ask() が終わるまでブロックする本物と同じ形。速度ではなく順序を再現する。"""

    def __init__(self, answer="答え", delay=0.0, error=None, deltas=()):
        self.answer, self.delay, self.error, self.deltas = answer, delay, error, deltas
        self.asked = []
        self.closed = False

    # **kw で受ける。本物の Conversation.ask に引数が増えるたびに
    # 偽物が TypeError を出し、ドライバがそれを『経路の失敗』として記録するので、
    # 症状が実装の欠陥そっくりに見える（実際そうなった）。
    def ask(self, text, *, connect, on_text=None, catalogue=None, protocol="",
            run_tool=None, **kw):
        self.asked.append(text)
        for d in self.deltas:
            if on_text:
                on_text(d)
            time.sleep(self.delay)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer

    def close(self):
        self.closed = True


def _drv(**kw):
    return SD.CopilotSocketDriver(_FakeConv(**kw), connect=object())


def _settle(drv, timeout=5.0):
    end = time.time() + timeout
    while drv._is_generating() and time.time() < end:
        time.sleep(0.01)
    return not drv._is_generating()


def test_send_returns_before_the_turn_finishes():
    """タブ用ドライバも send は投げるだけ。ここがブロックすると round-robin が止まり、
    concurrency>1 で他のワーカーが飢える。"""
    drv = _drv(delay=0.15)
    t0 = time.time()
    drv.send("質問")
    assert time.time() - t0 < 0.1
    assert drv._is_generating()
    assert _settle(drv)


def test_an_answer_is_counted_only_when_it_is_complete():
    """ループは _answers().count() の増加で『新しい返答が来た』と判断する。
    途中で増やすと未完成のテキストが確定回答として commit される。"""
    drv = _drv(delay=0.1)
    assert drv._answers().count() == 0
    drv.send("q")
    assert drv._answers().count() == 0
    assert _settle(drv)
    assert drv._answers().count() == 1


def test_the_partial_is_visible_while_the_turn_runs():
    drv = _drv(delay=0.05, deltas=("16", "166"), answer="166")
    drv.send("q")
    seen = set()
    end = time.time() + 3
    while drv._is_generating() and time.time() < end:
        seen.add(drv.read_last_response())
        time.sleep(0.01)
    assert _settle(drv)
    assert seen & {"16", "166"}
    assert drv.read_last_response() == "166"


def test_two_turns_at_once_are_refused_the_way_the_tab_driver_refuses_them():
    drv = _drv(delay=0.2)
    drv.send("one")
    with pytest.raises(GenerationInProgress):
        drv.send("two")
    assert _settle(drv)


def test_a_failed_turn_is_recorded_and_not_raised_into_the_loop():
    """経路が落ちることと仕事が落ちることは別。呼び出し側は failed を見てタブに切り替える。"""
    drv = SD.CopilotSocketDriver(_FakeConv(error=RuntimeError("socket gone")),
                                 connect=object())
    drv.send("q")
    assert _settle(drv)
    assert "socket gone" in drv.failed
    assert drv._answers().count() == 0          # 失敗は回答ではない


def test_a_failed_route_refuses_the_next_turn_instead_of_pretending():
    drv = SD.CopilotSocketDriver(_FakeConv(error=RuntimeError("gone")), connect=object())
    drv.send("q")
    assert _settle(drv)
    with pytest.raises(Exception) as exc:
        drv.send("again")
    assert "gone" in str(exc.value)


# ---- 引用の始末 ----------------------------------------------------------------------------

def test_the_citation_plumbing_the_browser_would_have_rendered_is_removed():
    """実測された生の応答:
    `**166**  166 tools available.【1-abc】  【1-abc】: cite:1 "Citation-1"`
    タブでは解決されてリンクになる部分で、決定機構やトランスクリプトに出す物ではない。"""
    raw = '**166**\n166 tools available.【1-abc】\n【1-abc】: cite:1 "Citation-1"'
    assert SD.normalize_answer(raw) == "**166**\n166 tools available."


def test_japanese_lenticular_brackets_survive():
    """【】は日本語の普通の括弧でもある。機械の形をしたものだけ落とす。"""
    text = "【重要】この手順は必ず守ってください。"
    assert SD.normalize_answer(text) == text


def test_normalising_never_invents_or_reorders_text():
    assert SD.normalize_answer("") == ""
    assert SD.normalize_answer("答えは 42 です。") == "答えは 42 です。"


# ---- ループが呼ぶ名前が全部あること ---------------------------------------------------------

def test_it_answers_to_every_name_the_turn_loop_calls():
    """ループは getattr で防御しているものとしていないものがある。
    欠けていた名前は本番でしか出ない -- ここで列挙して落とす。"""
    drv = _drv()
    for name in ("send", "_answers", "read_last_response", "read_last_reply_clean",
                 "_is_generating", "_is_stale_repeat", "_accept_new_reply",
                 "conversation_title", "response_block_count", "close"):
        assert callable(getattr(drv, name)), name
    for attr in ("_count_before", "answer_content_reads", "_last_returned_reply", "failed"):
        assert hasattr(drv, attr), attr


def test_the_same_turn_asked_about_twice_is_recognised():
    """ループが『もう受け取ったターン』についてまた尋ねている場合は True。

    実測 2026-08-21: 別の分岐が判定後にワーカーを 'waiting' のまま残すため、次の掃引が
    同じ回答を読み直して再判定していた。タブ側はこのガードで止まっていたが、socket 側は
    常に False を返していたので素通りし、unlock の試行4回が8秒で溶けた。"""
    drv = _drv(answer="166")
    drv.send("q")
    assert _settle(drv)
    assert drv._is_stale_repeat("166") is False, "まだ受け取っていないうちは stale ではない"
    drv._accept_new_reply("166")
    assert drv._is_stale_repeat("166") is True, "受け取ったターンをまた尋ねている"


def test_a_later_turn_saying_the_same_thing_is_a_new_answer():
    """2つの異なるターンがたまたま同じ文字列を返すことはある。
    それを抑止すると本物のターンを失う -- タブ側の問いとは別物であることの要。"""
    drv = SD.CopilotSocketDriver(_FakeConv(answer="166"), connect=object())
    drv.send("q")
    assert _settle(drv)
    drv._accept_new_reply("166")
    drv.send("q2")                       # 新しいターンが完了する
    assert _settle(drv)
    assert drv._is_stale_repeat("166") is False


def test_nothing_accepted_yet_is_never_stale():
    drv = _drv(answer="166")
    assert drv._is_stale_repeat("166") is False
    assert drv._is_stale_repeat("") is False


def test_closing_the_driver_closes_the_conversation():
    conv = _FakeConv()
    SD.CopilotSocketDriver(conv, connect=object()).close()
    assert conv.closed


def test_the_send_thread_does_not_outlive_the_answer():
    """デーモンスレッドを撒き散らすと、終わったはずのワーカーが生き続ける。"""
    before = threading.active_count()
    drv = _drv(delay=0.05)
    drv.send("q")
    assert _settle(drv)
    time.sleep(0.05)
    assert threading.active_count() <= before + 1


def test_a_completed_turn_with_no_text_falls_back_instead_of_counting():
    """ツール承認カードは Chat ではない別の messageType で届く。socket は押せない。
    『成功したが本文が無い』を回答として数えると、押されないカードを待ち続ける。"""
    drv = SD.CopilotSocketDriver(_FakeConv(answer="   "), connect=object())
    drv.send("q")
    assert _settle(drv)
    assert drv._answers().count() == 0
    assert "no text" in drv.failed


def test_an_empty_turn_names_the_frames_that_did_arrive():
    """空の回答は、いまは「空だった」しか言わない。バックエンドは tool 認可と確認を
    独自のメッセージ種別で送ってくるので、同意カードを載せて本文が無い完了ターンと、
    ただ何も言わなかったモデルとが、記録上で区別できなかった。

    どの種別が失効時に出るかは、同意を意図的に切る試験をするまで分からない。
    だから断定せず、来た種別をそのまま残す。"""
    class _Conv:
        def ask(self, text, *, connect, on_text=None, on_progress=None, **kw):
            if on_progress:
                on_progress({"type": "InternalSearchQuery", "origin": ""})
                on_progress({"type": "Progress", "origin": "ChainOfThoughtSummary"})
            return ""      # 完了したが本文なし

    d = SD.CopilotSocketDriver(_Conv(), connect=lambda *a, **k: None)
    d.send("hi")
    d._thread.join(timeout=5)
    assert d.failed
    assert "InternalSearchQuery" in d.failed
    assert "Progress/ChainOfThoughtSummary" in d.failed


def test_an_empty_turn_with_nothing_else_says_that_too():
    """種別も来なかったのなら、それも所見。空欄と区別する。"""
    class _Conv:
        def ask(self, text, *, connect, on_text=None, on_progress=None, **kw):
            return ""

    d = SD.CopilotSocketDriver(_Conv(), connect=lambda *a, **k: None)
    d.send("hi")
    d._thread.join(timeout=5)
    assert "no non-chat frames either" in d.failed



# ---- wait_for_idle: ブリッジがタブを手放すために唯一足りなかったもの ---------------------------

def test_wait_for_idle_returns_true_when_nothing_is_running():
    """ターンを一度も送っていないドライバは idle。join() ではここが特別扱いになる。"""
    assert _drv().wait_for_idle(timeout_s=0.1) is True


def test_wait_for_idle_blocks_until_the_turn_finishes():
    d = _drv(answer="答え", delay=0.4)
    d.send("q")
    assert d.wait_for_idle(timeout_s=0.05) is False, "走行中に idle と答えている"
    assert d.wait_for_idle(timeout_s=5) is True
    assert d.read_last_response() == "答え"


def test_a_timeout_is_not_reported_as_a_failure():
    """False は『まだ走っている』であって『壊れた』ではない。壊れたかは failed が答える。"""
    d = _drv(delay=0.4)
    d.send("q")
    assert d.wait_for_idle(timeout_s=0.05) is False
    assert not d.failed
    assert _settle(d)


def test_the_bridge_needs_exactly_these_four_calls():
    """ブリッジが DRIVER に呼ぶのは send / _is_generating / read_last_response /
    wait_for_idle の4つだけ。1つでも欠けると常駐ページを手放せない。"""
    for name in ("send", "_is_generating", "read_last_response", "wait_for_idle"):
        assert callable(getattr(SD.CopilotSocketDriver, name, None)), name


# ---- partial と settled: ブリッジの LOADING / LASTMSG に対応する2状態 --------------------------

def test_partial_grows_while_settled_stays_empty():
    """ブリッジの終了判定は『LASTMSG が埋まる』ことに依る。走行中に settled が
    答えを返してしまうと、末尾を切り落として完了扱いにする。"""
    d = _drv(answer="最終形", delay=0.25, deltas=("途", "途中"))
    d.send("q")
    seen_partial = ""
    while d._is_generating():
        seen_partial = seen_partial or d.partial_text()
        assert d.settled_text() == "", "走行中に settled が答えを返している"
        time.sleep(0.02)
    assert seen_partial, "partial が一度も見えていない"
    assert d.settled_text() == "最終形"


def test_partial_is_empty_before_the_first_token():
    assert _drv().partial_text() == ""


def test_settled_is_empty_before_any_turn():
    assert _drv().settled_text() == ""


def test_the_citation_plumbing_is_stripped_from_the_partial_too():
    """DOM は引用をリンクに解決して見せる。ソケットは生で渡すので、
    片方だけ整形すると利用者の画面に機械が漏れる。"""
    d = _drv(answer="本文", delay=0.2, deltas=("本文【1-a1b2】",))
    d.send("q")
    got = ""
    while d._is_generating():
        got = got or d.partial_text()
        time.sleep(0.02)
    _settle(d)
    assert "【1-a1b2】" not in got, got


# ---- 前のターンの回答を、次のターンの答えとして出さないこと ------------------------------------
#
# 外部レビューの指摘。`send` は `_partial` しか消しておらず、`_last` は残っていた。
# ブリッジのループは「生成中でない」＋「settled が非空」＋「1.2秒安定」で完了と見なすので、
# 2ターン目が失敗するとスレッドが死に、生成中でなくなり、settled が**前回の回答**を返し、
# ループはそれを新しい質問への答えとして確定した。完全で、安定していて、間違っている。

def test_a_new_turn_does_not_show_the_previous_answer():
    d = _drv(answer="最初の答え")
    d.send("q1"); assert _settle(d)
    assert d.settled_text() == "最初の答え"

    d.conv.answer = "二番目の答え"
    d.conv.delay = 0.4
    d.send("q2")
    assert d.partial_text() == "", "前ターンの本文が partial として出ている"
    assert d.settled_text() == "", "走行中に前ターンの答えを settled として出している"
    assert _settle(d)
    assert d.settled_text() == "二番目の答え"


def test_a_failed_turn_does_not_inherit_the_previous_answer():
    """これが一番危ない形: 失敗したターンが、前回の答えを『完成した答え』として差し出す。"""
    d = _drv(answer="最初の答え")
    d.send("q1"); assert _settle(d)

    d.conv.error = RuntimeError("socket died")
    d.send("q2"); assert _settle(d)
    assert d.failed, "失敗が記録されていない"
    assert d.settled_text() == "", "死んだターンが前回の答えを返している"
    assert d.read_last_response() == ""


def test_the_first_failed_turn_has_nothing_to_offer_either():
    d = _drv(error=RuntimeError("boom"))
    d.send("q"); assert _settle(d)
    assert d.settled_text() == "" and d.partial_text() == ""
