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


#: A completed turn. The reply names something from the prompt below, because a reply that
#: shares nothing with its prompt is now itself a finding -- a delivery failure -- and these
#: tests are about other things.
_PROMPT = "edit mod_b.py in C:/wd and report"
_DONE = ('data: {"replace": "done, mod_b.py updated"}\n\n'
         'event: done\ndata: {}\n\n')


def test_a_completed_turn_returns_its_answer():
    assert _Bridge(_DONE)(_PROMPT, "C:/wd") == "done, mod_b.py updated"


def test_a_stream_that_ended_without_done_is_an_environment_result():
    """3回走らせて 13/22・6/22・8/22、22件中19件が判定を反転した。
    失敗回は24-25秒/46-50秒に固まり、produced:"" / X not created /
    calls_through_the_api:0 -- 誤答ではなく『ターンが起きていない』署名。
    ブリッジは必ず done を出すので、その不在は未完了を意味する。"""
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge('data: {"delta": "half an ans')(_PROMPT, "C:/wd")
    assert "did not complete" in str(exc.value)


def test_an_empty_stream_is_not_graded_as_an_empty_answer():
    """空返答をゼロ点にすると、環境障害が能力の低下として記録される。"""
    with pytest.raises(A.TurnDidNotSettle):
        _Bridge("")(_PROMPT, "C:/wd")


def test_a_bridge_busy_for_the_whole_window_is_an_environment_result(monkeypatch):
    """混雑で1ターンも走らなかったのは、能力ではなく環境の結果。"""
    monkeypatch.setattr(A.BridgeAgent, "BUSY_RETRY_S", 0.01)
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge('{"ok": false, "error": "busy"}', retry_busy_s=0.05)(_PROMPT, "C:/wd")
    assert "busy" in str(exc.value)


def test_a_zero_retry_window_still_asks_once(monkeypatch):
    """締切を先に見ていたので retry_busy_s=0 は『一度も聞かない』を意味していた。"""
    monkeypatch.setattr(A.BridgeAgent, "BUSY_RETRY_S", 0.01)
    b = _Bridge(_DONE, retry_busy_s=0)
    assert b(_PROMPT, "C:/wd") == "done, mod_b.py updated"


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
        _NoNew(_DONE)(_PROMPT, "C:/wd")
    assert "order-dependent" in str(exc.value)


def test_a_bridge_error_that_terminated_the_stream_is_not_an_answer():
    """ブリッジは自分の例外の後にも done を出す。settled だけでは足りない。"""
    raw = 'data: {"ok": false, "error": "page went away"}\n\nevent: done\ndata: {}\n\n'
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge(raw)(_PROMPT, "C:/wd")
    assert "reported an error" in str(exc.value)


def test_an_answer_that_merely_mentions_errors_is_still_an_answer():
    """本文に error という語が出るだけで infra に落とすと、本物の回答が消える。"""
    raw = ('data: {"replace": "Common causes: an error in the config, ok: false in the log"}'
           '\n\nevent: done\ndata: {}\n\n')
    assert "Common causes" in _Bridge(raw)(_PROMPT, "C:/wd")


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
        _Bridge(raw)(_PROMPT, "C:/wd")
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
    assert _Bridge(raw)("what is 6 times 7? answer with digits", "C:/wd") == "42"


# ---------------------------------------------------------------------------------------
# Delivery failures, found by printing the replies instead of inferring from latencies
# ---------------------------------------------------------------------------------------

def test_a_greeting_means_the_task_never_arrived():
    """観測された返答: 『こんにちは。ご用件をお聞かせください。何かお手伝いできることは
    ありますか？』 -- タスクがタブに届いていない。37文字、正常終了、そして
    『filesystem が苦手な系』として記録されていた。"""
    raw = ('data: {"replace": "こんにちは。ご用件をお聞かせください。'
           '何かお手伝いできることはありますか？"}\n\nevent: done\ndata: {}\n\n')
    b = _Bridge(raw)
    b("作業フォルダは C:/wd です。mod_b.py の TIMEOUT を変更してください。", "C:/wd")
    # FLAGGED, NOT EXCLUDED. Raising moves the turn out of the denominator and RAISES the pass
    # rate -- the direction every defect found in this suite has already moved it -- and the
    # same check calls a terse correct answer a delivery failure. So the suspicion is recorded
    # for a person to look at, and the turn is still counted.
    assert b.transcript[-1]["delivery_suspect"] is True


def test_a_terse_correct_answer_is_only_suspected_never_excluded():
    """『42』は短く、プロンプトと語を共有しない -- 捕まえたい挨拶文と同じ形。
    ここで例外にすると、正しい回答を分母から外すことになる。"""
    raw = 'data: {"replace": "42"}\n\nevent: done\ndata: {}\n\n'
    b = _Bridge(raw)
    assert b("what is 6 times 7? answer with digits", "C:/wd") == "42"
    assert b.transcript[-1]["delivery_suspect"] is True


def test_a_short_reply_that_names_the_file_is_an_attempt():
    """『完了しました。mod_b.py を変更しました』は着手の証拠。
    それが真実かどうかはグレーダの問いで、この関数の問いではない。"""
    raw = ('data: {"replace": "完了しました。mod_b.py の TIMEOUT を変更しました。"}'
           '\n\nevent: done\ndata: {}\n\n')
    assert _Bridge(raw)("mod_b.py の TIMEOUT を 30 から 90 に変更してください。", "C:/wd")


def test_a_long_answer_counts_as_an_attempt_even_without_shared_words():
    """言い換えただけの長い回答も回答。両方の条件を要求するのはこのため。"""
    assert A.attempted_the_task("edit mod_b.py in C:/wd", "x" * 200)


def test_the_check_is_positive_evidence_rather_than_a_phrase_list():
    """relay 側は語句一覧を持っていて、今回の挨拶文はそこに無かった。
    手書き一覧は fail-open し、取りこぼしが結果の形をして出てくる。"""
    from relay.copilot_autopilot_relay import _GOAL_NOT_SEEN_MARKERS
    greeting = "こんにちは。ご用件をお聞かせください。何かお手伝いできることはありますか？"
    assert not any(m in greeting for m in _GOAL_NOT_SEEN_MARKERS), \
        "前提が変わった: 一覧が捕捉するようになったなら、この試験の理由を書き直すこと"
    assert not A.attempted_the_task("edit mod_b.py in C:/wd", greeting)


def test_a_bridge_error_arriving_as_reply_text_is_not_an_answer():
    """観測: `[bridge error: RuntimeError: send failed: composer cleared ...]` が
    返答本文として届き、フレーム単位の検査は見ていなかった。"""
    raw = ('data: {"replace": "[bridge error: RuntimeError: send failed: composer cleared '
           'without a conversation or generation acknowledgement]"}'
           '\n\nevent: done\ndata: {}\n\n')
    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Bridge(raw)("edit mod_b.py in C:/wd", "C:/wd")
    assert "arrived as the reply text" in str(exc.value)


# ---- delivery, read back from the conversation ----------------------------------------------

def test_the_prompt_carries_a_marker_minted_for_the_turn():
    """workdir の変化は『その作業場所で何かが動いた』であって、
    プロンプトが会話に届いたことの証明ではない -- アダプタもそのパスを持っている。"""
    b = _Bridge(_DONE)
    b("edit mod_b.py", "C:/wd")
    prompt = b.transcript[-1]["prompt"]
    assert b.transcript[-1]["nonce"] in prompt
    assert prompt.rstrip().endswith("]"), "marker が末尾の独立行にない"


def test_two_turns_never_share_a_marker():
    b = _Bridge(_DONE)
    b("edit mod_b.py", "C:/wd")
    b("edit mod_b.py", "C:/wd")
    assert b.transcript[0]["nonce"] != b.transcript[1]["nonce"]


def test_a_history_the_bridge_could_not_serve_is_unknown_not_absent():
    """『履歴が busy だった』は『プロンプトが届かなかった』ではない。
    確認できなかったことを否定として記録すると、環境の不調が配送失敗として現れる。"""
    class _NoHistory(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                raise OSError("connection refused")
            return self._raw

    b = _NoHistory(_DONE)
    b("edit mod_b.py", "C:/wd")
    assert b.transcript[-1]["prompt_in_conversation"] is None


def test_a_conversation_without_the_marker_is_a_delivery_failure():
    class _Empty(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                return 'HTTP/1.1 200 OK\r\n\r\n{"ok": true, "url": "u", "messages": []}'
            return self._raw

    # The turn is RECORDED and then refused. Recording it is what leaves something to
    # diagnose with; refusing it is what keeps it out of the capability denominator.
    b = _Empty(_DONE)
    with pytest.raises(A.TurnDidNotSettle):
        b("edit mod_b.py", "C:/wd")
    assert b.transcript[-1]["prompt_in_conversation"] is False


def test_a_conversation_carrying_the_marker_confirms_delivery():
    class _Has(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                nonce = self.transcript[-1]["nonce"] if self.transcript else ""
                return ('HTTP/1.1 200 OK\r\n\r\n{"ok": true, "url": "u", "messages": '
                        '[{"role": "user", "text": "... %s"}]}' % nonce)
            return self._raw

    b = _Has(_DONE)
    # the history call happens before the transcript append, so drive it directly
    b.transcript.append({"nonce": "cb-turn-abc"})
    got = b._confirm_delivered("cb-turn-abc")
    assert got["delivered"] is True


def test_the_history_check_retries_past_a_busy_bridge(monkeypatch):
    """/history はページロックを要る。ターン直後に聞くので最初の試行は busy に当たりやすく、
    最初の診断では10ターン中4件が『確認できず』になった。半分近く答えない検査は検査ではない。"""
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    class _BusyThenOk(_Bridge):
        def _request(self, path, timeout=None):
            if path != "/history":
                return self._raw
            calls["n"] += 1
            if calls["n"] < 3:
                return 'HTTP/1.1 200 OK\r\n\r\n{"ok": false, "error": "busy"}'
            return ('HTTP/1.1 200 OK\r\n\r\n{"ok": true, "url": "u", "messages": '
                    '[{"role": "user", "text": "... %s"}]}' % self._nonce)

    b = _BusyThenOk(_DONE)
    b._nonce = "cb-turn-xyz"
    got = b._confirm_delivered("cb-turn-xyz")
    assert got["delivered"] is True
    assert calls["n"] == 3


def test_a_non_busy_error_is_not_retried(monkeypatch):
    """混雑以外の失敗を6回繰り返しても答えは変わらず、ターンごとに待ち時間が増えるだけ。"""
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    class _Broken(_Bridge):
        def _request(self, path, timeout=None):
            if path != "/history":
                return self._raw
            calls["n"] += 1
            return 'HTTP/1.1 200 OK\r\n\r\n{"ok": false, "error": "page is gone"}'

    got = _Broken(_DONE)._confirm_delivered("cb-turn-xyz")
    assert got["delivered"] is None and calls["n"] == 1


def test_a_turn_missing_from_its_conversation_is_the_harness_not_the_answer():
    """4エピソード×5反復=20ターン。合格11件は全て会話にプロンプトがあり、
    失敗9件のうち8件は無かった。未配送の失敗は返答8〜113文字、
    配送済みの失敗は608文字。確認は20/20で成立している。"""
    class _Missing(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                return 'HTTP/1.1 200 OK\r\n\r\n{"ok": true, "url": "u", "messages": []}'
            return self._raw

    with pytest.raises(A.TurnDidNotSettle) as exc:
        _Missing(_DONE)(_PROMPT, "C:/wd")
    assert "not in the conversation" in str(exc.value)


def test_the_rejected_turn_is_still_recorded_before_it_raises():
    """診断できない却下は、次に同じものを探すときの手掛かりを消す。"""
    class _Missing(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                return 'HTTP/1.1 200 OK\r\n\r\n{"ok": true, "url": "u", "messages": []}'
            return self._raw

    b = _Missing(_DONE)
    with pytest.raises(A.TurnDidNotSettle):
        b(_PROMPT, "C:/wd")
    assert b.transcript[-1]["prompt_in_conversation"] is False
    assert b.transcript[-1]["nonce"]


def test_a_turn_the_check_could_not_answer_is_still_graded():
    """『履歴が答えられなかった』で採点をやめると、ブリッジの不調が
    そのまま能力の分母を削る -- まさにこの一連で繰り返し見つけた形。"""
    class _NoHistory(_Bridge):
        def _request(self, path, timeout=None):
            if path == "/history":
                raise OSError("refused")
            return self._raw

    b = _NoHistory(_DONE)
    assert b(_PROMPT, "C:/wd") == "done, mod_b.py updated"
    assert b.transcript[-1]["prompt_in_conversation"] is None
