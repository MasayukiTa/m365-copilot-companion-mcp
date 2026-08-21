"""捕獲は艦隊全体の単一障害点。1回の送信レースで経路ごと落ちてはいけない。

実測 (2026-08-21): 実走の1回目で `composer cleared without a conversation or generation
acknowledgement` が出て捕獲が失敗し、その run は全ワーカーがタブになった。
タブ経路自身がリトライで回避している既知のレースを、捕獲だけが素通しにしていた。
"""
import pytest

from relay import chathub_capture as CC
from relay.chathub import ChatHubError


def test_a_flaky_attempt_costs_a_retry_not_the_route(monkeypatch):
    calls = []

    def once(page, *, prompt, timeout_s):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("send failed: composer cleared without a conversation")
        return ("tok", "tpl")

    monkeypatch.setattr(CC, "_capture_once", once)
    assert CC.capture(object()) == ("tok", "tpl")
    assert len(calls) == 3


def test_a_capture_that_never_works_gives_up_and_says_how_often_it_tried(monkeypatch):
    """無限に粘ると、経路が死んでいる run で全ワーカーが待たされる。
    諦めること自体が仕様であって、諦め方が観測できることも仕様。"""
    def never(page, *, prompt, timeout_s):
        raise RuntimeError("composer cleared")

    monkeypatch.setattr(CC, "_capture_once", never)
    with pytest.raises(ChatHubError) as exc:
        CC.capture(object(), attempts=2)
    assert "after 2 attempts" in str(exc.value)
    assert "composer cleared" in str(exc.value)


def test_a_capture_that_works_first_time_does_not_retry(monkeypatch):
    """捕獲は実ターン1回ぶんの費用がかかる。成功しているのに繰り返すのは無駄な課金。"""
    calls = []

    def once(page, *, prompt, timeout_s):
        calls.append(1)
        return ("tok", "tpl")

    monkeypatch.setattr(CC, "_capture_once", once)
    CC.capture(object())
    assert len(calls) == 1


def test_attempts_is_never_zero_however_it_is_asked_for(monkeypatch):
    """attempts=0 を『捕獲しない』と解釈すると、静かに経路が消える。"""
    calls = []
    monkeypatch.setattr(CC, "_capture_once",
                        lambda page, *, prompt, timeout_s: calls.append(1) or ("t", "p"))
    CC.capture(object(), attempts=0)
    assert len(calls) == 1


def test_a_structural_failure_is_not_retried(monkeypatch):
    """『このタブはエージェント面ではない』は何回やっても同じ結果になる。
    捕獲1回は実ターン1回ぶんの費用なので、3回繰り返すのは3ターンの浪費。
    実際にそうなった -- 最初の再試行実装がこれをやった。"""
    calls = []

    def structural(page, *, prompt, timeout_s):
        calls.append(1)
        raise CC.NotAnAgentSurface("names no agent")

    monkeypatch.setattr(CC, "_capture_once", structural)
    with pytest.raises(CC.NotAnAgentSurface):
        CC.capture(object(), attempts=5)
    assert len(calls) == 1


def test_a_structural_failure_still_reads_as_a_capture_failure_to_the_caller():
    """呼び出し側 (SocketRoute.refresh) は ChatHubError を捕まえてタブに落ちる。
    新しい型がその網から漏れると、経路の失敗が例外になって艦隊まで届く。"""
    assert issubclass(CC.NotAnAgentSurface, ChatHubError)
