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

    def ask(self, text, *, connect, on_text=None, catalogue=None, protocol="", run_tool=None):
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


def test_an_identical_answer_twice_is_two_answers_not_a_stale_capture():
    """タブ側の stale 判定は『読み手が前ターンの DOM を返した』事故への対策。
    socket では走っているターン自身がテキストを作るので、同じ答えの抑止は
    本物のターンを失うことにしかならない。"""
    drv = _drv(answer="166")
    drv.send("q")
    assert _settle(drv)
    assert drv._is_stale_repeat("166") is False


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
