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
import bench.companionbench  # noqa: F401
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
