"""The agent adapters, and the SSE trap that cost most of a day.

The bridge answers as text/event-stream with keep-alive and periodic `: ping` frames, so a
client that waits for EOF blocks forever after the answer has already arrived. Five "hangs"
were measured that way -- 954s, 949s, 448s, 428s, 443s -- against a bridge answering in
about 28 seconds, and three product changes were attributed to it before the client turned
out to be the fault.

These tests pin the two things that prevent a rediscovery: the request says
Connection: close, and the read stops at `event: done`. Neither is verifiable by running
the adapter (a healthy bridge hides the bug), so they are asserted against the source and
the parser directly.
"""
import pytest

import bench.companionbench  # noqa: F401
import bench.companionbench.agents as A
from bench.companionbench.agents import BridgeAgent, SimulatedAgent, bridge_available


def test_the_request_closes_the_connection_rather_than_waiting_for_eof():
    import inspect
    src = inspect.getsource(BridgeAgent._request)
    assert "Connection: close" in src
    assert 'b"event: done"' in src, "done で止めていない＝応答後も読み続ける"


def test_the_settled_answer_wins_over_the_streamed_deltas():
    raw = ('data: {"delta": "計算"}\n'
           'data: {"delta": "中..."}\n'
           'data: {"replace": "132000"}\n'
           'event: done\n')
    assert BridgeAgent._answer(raw) == "132000"


def test_deltas_are_used_when_a_turn_streamed_but_never_settled():
    raw = 'data: {"delta": "部分"}\ndata: {"delta": "回答"}\n'
    assert BridgeAgent._answer(raw) == "部分回答"


def test_ping_frames_and_malformed_lines_are_ignored():
    raw = (': ping\n\ndata: not json\n: ping\n'
           'data: {"replace": "ok"}\nevent: done\n')
    assert BridgeAgent._answer(raw) == "ok"


def test_an_empty_body_yields_an_empty_answer_not_an_exception():
    assert BridgeAgent._answer("") == ""


def test_the_prompt_carries_the_workdir():
    """一時フォルダの絶対パスを渡さないと、エージェントは別の場所で作業する。"""
    import inspect
    src = inspect.getsource(BridgeAgent.__call__)
    assert "作業フォルダは %s" in src


def test_a_missing_bridge_is_reported_not_raised():
    """ブリッジ不在は環境の事実。能力の測定結果にしてはならない。"""
    assert bridge_available(port=1) is False


def test_the_simulated_agent_runs_the_script_for_its_episode():
    seen = {}

    def do(workdir):
        seen["workdir"] = workdir
        return "done"

    sim = SimulatedAgent({"e1": do})
    assert sim.for_episode("e1")("prompt", "/tmp/x") == "done"
    assert seen["workdir"] == "/tmp/x"
    assert sim.calls[0]["episode_id"] == "e1"


def test_an_unscripted_episode_does_nothing_and_says_so():
    """台本の無いエピソードを勝手に成功させない。不合格が正しい。"""
    sim = SimulatedAgent({})
    assert "no action" in sim.for_episode("unknown")("p", "/tmp/x")


# ---------------------------------------------------------------------------------------
# A turn that did not complete is not a wrong answer
# ---------------------------------------------------------------------------------------

class _Bridge(A.BridgeAgent):
    """A BridgeAgent whose transport returns a canned SSE body."""

    def __init__(self, raw, **kw):
        super().__init__(**kw)
        self._raw = raw

    def _request(self, path, timeout=None):
        return self._raw

    def _new_conversation(self):
        return True


_DONE = ('data: {"replace": "the answer"}\n\n'
         'event: done\ndata: {}\n\n')


def test_a_completed_turn_returns_its_answer():
    assert _Bridge(_DONE)("do the thing", "C:/wd") == "the answer"


def test_a_stream_that_ended_without_done_is_an_environment_result():
    """3回走らせて 13/22・6/22・8/22、22件中19件が判定を反転した。
    失敗回は24-25秒/46-50秒に固まり、produced:"" / X not created /
    calls_through_the_api:0 -- 誤答ではなく『ターンが起きていない』署名。
    ブリッジは必ず done を出すので、その不在は未完了を意味する。"""
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge('data: {"delta": "half an ans')("do the thing", "C:/wd")
    assert "did not complete" in str(exc.value)


def test_an_empty_stream_is_not_graded_as_an_empty_answer():
    """空返答をゼロ点にすると、環境障害が能力の低下として記録される。"""
    with pytest.raises(A.TurnDidNotSettle):
        _Bridge("")("do the thing", "C:/wd")


def test_a_bridge_busy_for_the_whole_window_is_an_environment_result(monkeypatch):
    """混雑で1ターンも走らなかったのは、能力ではなく環境の結果。"""
    monkeypatch.setattr(A.BridgeAgent, "BUSY_RETRY_S", 0.01)
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge('{"ok": false, "error": "busy"}', retry_busy_s=0.05)("x", "C:/wd")
    assert "busy" in str(exc.value)


def test_a_zero_retry_window_still_asks_once(monkeypatch):
    """締切を先に見ていたので retry_busy_s=0 は『一度も聞かない』を意味していた。"""
    monkeypatch.setattr(A.BridgeAgent, "BUSY_RETRY_S", 0.01)
    b = _Bridge(_DONE, retry_busy_s=0)
    assert b("x", "C:/wd") == "the answer"


def test_a_settled_but_empty_answer_is_still_graded():
    """完了したのに何も書かなかったのは、環境ではなく能力の結果。
    ここを infra に逃がすと、本物の失敗が分母から消える。"""
    assert _Bridge('event: done\ndata: {}\n\n')("x", "C:/wd") == ""


def test_the_runner_records_a_non_settling_turn_as_infra():
    """例外 -> INFRA は既存の仕組み。分母から外れることが要点。"""
    from bench.companionbench import runner as R
    from bench.companionbench.pools import EVOLUTION, REGISTRY
    import bench.companionbench.episodes  # noqa: F401

    ep = REGISTRY.get(EVOLUTION)[0]
    out = R.run_episode(ep, _Bridge(""))
    assert out["infra_failure"] is True
    assert out["success"] is False


def test_a_failed_fresh_conversation_is_an_environment_result_not_a_silent_carry_over():
    """戻り値を捨てていたので、ブリッジが混雑していると前のエピソードの会話の中で走った。
    一方のエピソードが他方の文脈を持つのは、この adapter が新規会話を開く理由そのもの。
    しかも採点は通るので、スイートの答えが実行順に依存する。"""
    class _NoNew(_Bridge):
        def _new_conversation(self):
            return False

    with pytest.raises(A.TurnDidNotSettle) as exc:
        _NoNew(_DONE)("x", "C:/wd")
    assert "order-dependent" in str(exc.value)


def test_a_bridge_error_that_terminated_the_stream_is_not_an_answer():
    """ブリッジは自分の例外の後にも done を出す。settled だけでは足りない。"""
    raw = 'data: {"ok": false, "error": "page went away"}\n\nevent: done\ndata: {}\n\n'
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge(raw)("x", "C:/wd")
    assert "reported an error" in str(exc.value)


def test_an_answer_that_merely_mentions_errors_is_still_an_answer():
    """本文に error という語が出るだけで infra に落とすと、本物の回答が消える。"""
    raw = ('data: {"replace": "Common causes: an error in the config, ok: false in the log"}'
           '\n\nevent: done\ndata: {}\n\n')
    assert "Common causes" in _Bridge(raw)("x", "C:/wd")


def test_the_client_does_not_give_up_before_the_bridge_does():
    """クライアントが先に諦めると、遅いターンが infra になって分母から消える --
    対象が遅くなるほど pass rate が上がる。"""
    from bench.companionbench.agents import BridgeAgent
    assert BridgeAgent().timeout >= 600


def test_a_rate_limit_notice_is_the_environment_not_a_wrong_answer():
    """3回の実行が 7/22・17/21・19/22 で、最悪の回が最速だった(中央値50秒 対 73秒/77秒)。
    速くて落ちるのは『何もせずに答えた』署名で、観測された文面はレート制限の通知。
    正常に done で終わるので、それまでの分類を素通りして能力の失敗として記録されていた。"""
    raw = ('data: {"replace": "この量のリクエストには、現在一時的に応答できません。'
           '後でもう一度お試しください。"}\n\nevent: done\ndata: {}\n\n')
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge(raw)("x", "C:/wd")
    assert "declined this turn" in str(exc.value)


def test_a_long_answer_that_discusses_rate_limiting_is_still_an_answer():
    """レート制限について説明した回答まで infra にすると、
    系に都合のよい方向で本物の証拠が消える。"""
    body = ("Rate limiting protects a service from too many requests. " * 6)
    raw = 'data: {"replace": "%s"}\n\nevent: done\ndata: {}\n\n' % body
    assert "Rate limiting protects" in _Bridge(raw)("x", "C:/wd")


def test_the_declined_check_is_case_insensitive_and_covers_english():
    assert A.service_declined("Too Many Requests")
    assert A.service_declined("Please try again later.")
    assert not A.service_declined("the report is ready")


def test_degraded_throttling_is_knowingly_not_detected():
    """スロットルが『拒否』で来れば拾えるが、『品質低下』で来ると
    能力の失敗と区別がつかない。非対称な取りこぼしが残ることを、
    検査のある場所に書いておく -- 別の場所に書いた但し書きは数字と一緒に旅をしない。"""
    import inspect
    doc = inspect.getdoc(A.service_declined)
    assert "DEGRADATION" in doc
    assert "one-sided" in doc
    # and it behaves that way: a short, plausible, wrong answer is still an answer
    raw = 'data: {"replace": "42"}\n\nevent: done\ndata: {}\n\n'
    assert _Bridge(raw)("x", "C:/wd") == "42"
