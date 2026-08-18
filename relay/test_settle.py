"""「返信が完了したか」の一つの規則。純関数なのでドライバも実時計も要らない。

この判定は現在4箇所に別実装されており、3,931返信の実測が正当化したガードを
持っているのは1つだけ。残り3つのうち2つには「同じガードを直接適用している」と
コメントが書かれている。適用していない。コメントは機構ではない。

最弱は refuter のもので、サンプル数もマーカー概念も無い -- 書きかけの反証文が
判定として受理され得る。それが best-of-N のセレクタであり、プロジェクト自身の記述が
出力品質の上限と認めている当のコンポーネント。
"""
import pytest

from relay import settle as S


class _Clock:
    """時刻は引数なので、進めるのは足し算だけ。実待ちゼロで dwell 演算を厳密に回せる。"""

    def __init__(self, start=0.0):
        self.t = start

    def advance(self, s):
        self.t += s
        return self.t


def _drive(texts, *, dwell_s=2.0, tick=1.0, samples=3, marker=True,
           generating=False, processing=False, start=0.0):
    """テキスト列を1ポーリングずつ流し、(outcome, state) の履歴を返す。"""
    clock = _Clock(start)
    state = S.SettleState()
    out = []
    for text in texts:
        state, outcome = S.settle_step(
            state, text, now=clock.t, dwell_s=dwell_s, generating=generating,
            is_processing=processing, has_marker=marker, samples=samples)
        out.append((outcome, state))
        clock.advance(tick)
    return out


# ---- 成長するテキストは最終形だけが受理される -----------------------------------------------

def test_a_growing_reply_is_never_accepted_mid_stream():
    """途中形が受理されると、mid-word で切れた 102 文字が「最終回答」になる。
    実際にそうなった -- ストリーミングの一時停止が dwell より長かったため。"""
    growing = ["takeuchi", "takeuchifile操作", "takeuchifile操作\nリ"]
    outcomes = [o for o, _ in _drive(growing)]
    assert outcomes == [S.RESET, S.RESET, S.RESET]
    assert S.ACCEPT not in outcomes


def test_the_final_form_settles_once_it_stops_changing():
    final = "完成した回答です DONE"
    outcomes = [o for o, _ in _drive(["途中", final, final, final, final])]
    assert outcomes[-1] == S.ACCEPT
    assert outcomes[:2] == [S.RESET, S.RESET]


# ---- サンプル数と dwell の両方が要る ---------------------------------------------------------

def test_one_sighting_is_not_stability():
    """1回読んだだけは安定ではなく、単に1回読んだだけ。"""
    got = _drive(["x DONE"], samples=3)
    assert got[0][0] == S.RESET and got[0][1].stable_count == 1


def test_the_count_starts_at_one_not_zero():
    """新しいテキストを見たその読み取り自体が最初の観測。0 から始めると全箇所で
    ポーリングが1回余分に要り、off-by-one ではなく「ガードが厳しくなった」ように見える。"""
    _, state = _drive(["新しい本文"])[0]
    assert state.stable_count == 1 and state.stable_since == 0.0


def test_enough_samples_but_not_enough_dwell_keeps_waiting():
    """サンプルだけ満たして受理すると、速いポーリングが dwell を無効化する。"""
    got = _drive(["a DONE"] * 5, dwell_s=10.0, tick=0.1, samples=3)
    assert all(o in (S.RESET, S.WAITING) for o, _ in got)


def test_enough_dwell_but_not_enough_samples_keeps_waiting():
    got = _drive(["a DONE"] * 3, dwell_s=1.0, tick=60.0, samples=8)
    assert [o for o, _ in got] == [S.RESET, S.WAITING, S.WAITING]


# ---- マーカーが無い尾は両方を倍にする ---------------------------------------------------------

def test_a_markerless_tail_doubles_both_requirements():
    """Stop ボタンがストリーム中の2チャンク間で一瞬消えると、
    途中停止が安定に見える。マーカーが無いときだけ要求を倍にする根拠。"""
    assert S.requirements(dwell_s=2.0, has_marker=True, samples=3) == (3, 2.0)
    assert S.requirements(dwell_s=2.0, has_marker=False, samples=3) == (6, 4.0)


def test_the_same_sequence_settles_with_a_marker_and_does_not_without_one():
    """倍化が実際に効いていること -- 定数だけ見て満足しない。"""
    seq = ["本文"] * 4
    assert S.ACCEPT in [o for o, _ in _drive(seq, marker=True, samples=3, dwell_s=2.0)]
    assert S.ACCEPT not in [o for o, _ in _drive(seq, marker=False, samples=3, dwell_s=2.0)]


# ---- placeholder は reset ではなく skip --------------------------------------------------------

def test_a_processing_placeholder_does_not_destroy_accumulated_stability():
    """2026-08-10 実測: ブロックは 回答 -> 処理中です。 -> 空 -> 回答 を数秒周期で
    繰り返し、その間 Stop ボタンは不在。毎回リセットすると、既に終わっている
    ターンを無駄に引き延ばす。"""
    clock = _Clock()
    state = S.SettleState()
    state, first = S.settle_step(state, "回答 DONE", now=clock.t, dwell_s=1.0,
                                 generating=False, is_processing=False, has_marker=True,
                                 samples=2)
    clock.advance(1.0)
    state, second = S.settle_step(state, "処理中です。", now=clock.t, dwell_s=1.0,
                                  generating=False, is_processing=True, has_marker=True,
                                  samples=2)
    assert second == S.SKIP
    assert state.stable_count == 1 and state.last == "回答 DONE", (
        "placeholder が蓄積を壊した -- それが needless な引き延ばしの原因だった")
    clock.advance(1.0)
    state, third = S.settle_step(state, "回答 DONE", now=clock.t, dwell_s=1.0,
                                 generating=False, is_processing=False, has_marker=True,
                                 samples=2)
    assert third == S.ACCEPT


def test_a_placeholder_can_never_itself_be_accepted():
    """skip にしても、placeholder が最終回答になる経路が開いてはいけない。"""
    got = _drive(["処理中です。"] * 10, processing=True, samples=2, dwell_s=0.0)
    assert all(o == S.SKIP for o, _ in got)
    assert got[-1][1].last is None


def test_a_placeholder_while_generating_still_resets():
    """generating は無条件で権威ある「まだ続いている」信号。"""
    clock = _Clock()
    state = S.SettleState(last="回答", stable_count=5, stable_since=0.0)
    state, outcome = S.settle_step(state, "処理中です。", now=clock.t, dwell_s=1.0,
                                   generating=True, is_processing=True, has_marker=True)
    assert outcome == S.RESET and state.stable_count == 0 and state.last is None


def test_generating_resets_no_matter_how_stable_the_text_looked():
    state = S.SettleState(last="完成 DONE", stable_count=99, stable_since=0.0)
    state, outcome = S.settle_step(state, "完成 DONE", now=1000.0, dwell_s=1.0,
                                   generating=True, is_processing=False, has_marker=True)
    assert outcome == S.RESET and state == S.SettleState()


# ---- 時刻ゼロの罠 -------------------------------------------------------------------------------

def test_a_clock_reading_of_zero_is_a_time_not_an_unset_value():
    """`if state.stable_since:` だと 0.0 が未設定扱いになり、elapsed が永久に 0.0 に
    釘付けされる -- どれだけ静止していても settle しないターンができる。"""
    state = S.SettleState(last="a DONE", stable_count=2, stable_since=0.0)
    _, outcome = S.settle_step(state, "a DONE", now=100.0, dwell_s=2.0,
                               generating=False, is_processing=False, has_marker=True,
                               samples=3)
    assert outcome == S.ACCEPT


# ---- 述語の受け取りかた -------------------------------------------------------------------------

def test_a_predicate_may_be_a_callable_so_each_site_swaps_only_its_marker_rule():
    """各サイトの差はマーカー判定関数だけ、というのがこの統一の要点。"""
    marker = lambda t: t.rstrip().endswith("DONE")

    def first_accept(texts):
        outs = [o for o, _ in _drive(texts, marker=marker, samples=2, dwell_s=1.0)]
        return outs.index(S.ACCEPT) if S.ACCEPT in outs else None

    # 倍化は禁止ではなく遅延。受理の有無ではなく「いつ受理されるか」で測る --
    # ポーリング回数を多く取れば無マーカーでも最後には受理され、有無だけを見る
    # アサーションは、倍化が効いていなくても偶然通る。
    assert first_accept(["x DONE"] * 6) == 1
    assert first_accept(["x"] * 6) == 3


def test_a_non_boolean_predicate_is_a_caller_error_not_a_truthy_value():
    """`bool("false")` は True。この継ぎ目でそれが起きると、
    呼び出し側が「まだ処理中」と印を付けたつもりの返信が受理される。"""
    for bad in ("false", 1, None):
        with pytest.raises(TypeError):
            S.settle_step(S.SettleState(), "x", now=0.0, dwell_s=1.0, generating=False,
                          is_processing=bad, has_marker=True)


# ---- 状態は共有されない -------------------------------------------------------------------------

def test_the_state_is_immutable_so_two_loops_cannot_settle_each_others_turns():
    state = S.SettleState(last="a", stable_count=1, stable_since=0.0)
    S.settle_step(state, "a DONE", now=1.0, dwell_s=1.0, generating=False,
                  is_processing=False, has_marker=True)
    assert state.stable_count == 1 and state.last == "a"
    with pytest.raises(Exception):
        state.stable_count = 5


# ---- 説明できないタイムアウトを残さない ---------------------------------------------------------

def test_a_stuck_turn_can_say_what_it_was_short_of():
    """画面上は完成しているのに settle しないターンと、まだストリーミング中のターンは
    区別できない -- その曖昧さのせいで、元の truncation は特定に3,931返信を要した。"""
    state = S.SettleState(last="長い回答" * 50 + "末尾", stable_count=2, stable_since=10.0)
    got = S.explain(state, now=11.0, dwell_s=5.0, has_marker=False, samples=3)
    assert got["short_by_samples"] == 4 and got["short_by_seconds"] == 9.0
    assert got["tail"].endswith("末尾"), "切り詰めは末尾に出る -- 冒頭を載せても健康に見える"


def test_explain_reports_zero_shortfall_once_the_requirements_are_met():
    state = S.SettleState(last="x DONE", stable_count=3, stable_since=0.0)
    got = S.explain(state, now=10.0, dwell_s=2.0, has_marker=True, samples=3)
    assert got["short_by_samples"] == 0 and got["short_by_seconds"] == 0.0


# ---- 移行の進み具合 --------------------------------------------------------------------------

def test_which_sites_have_moved_and_which_are_still_gated():
    """統一の価値は最後のサイトが移るまで実現しない。表が古くなったら落ちる。

    正典サイトだけがゲート無し -- 規則の出所であり、移行が何も変えないことが要件だった。
    残りは採用そのものが挙動変更（どれもサンプル要求を持っていない）なので、
    A/B が勝ちを示すまで既定は旧経路。"""
    import io
    state = {"relay/copilot_autopilot_relay.py": "ungated",
             "relay/relay_fleet.py": "gated",
             "relay/refuter.py": "gated",
             "relay/agent_profiles.py": "not_yet"}
    for path, expected in state.items():
        src = io.open(path, encoding="utf-8").read()
        uses = "_settle.settle_step(" in src
        gated = "_settle.unified()" in src
        assert uses is (expected != "not_yet"), path
        assert gated is (expected == "gated"), path


def test_the_gate_is_off_by_default(monkeypatch):
    """既定で有効になった瞬間、A/B は「新実装 対 新実装」になる。

    測るのは出荷時の既定であって、いま走っている環境ではない -- 前者を測るつもりで
    後者を読むと、ゲートを1に立てて回した瞬間にこのテストが落ちて、
    「既定が変わった」と誤読される。"""
    monkeypatch.delenv("MCP_SETTLE_UNIFIED", raising=False)
    assert S.unified() is False
    for off in ("0", "", " 0 ", "no"):
        monkeypatch.setenv("MCP_SETTLE_UNIFIED", off)
        assert S.unified() is False, off
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "1")
    assert S.unified() is True


def test_the_gate_is_recorded_in_the_fingerprint():
    """未記録トグルは未記録交絡。オンの走行とオフの走行は別のハーネス。"""
    from relay.selfimprove import experiment as EX
    assert "MCP_SETTLE_UNIFIED" in EX.FINGERPRINT_ENV_KEYS
    assert "MCP_MARKERLESS_DWELL_FACTOR" in EX.FINGERPRINT_ENV_KEYS


# ---- 移行が「振る舞い不変」であるための2性質 ------------------------------------------------
#
# どちらも差分レビューで実際に見つかった欠陥。テストが無ければ、次の移行（手順3〜5）で
# 同じ形が再発しても誰も気づかない。

def test_the_marker_rule_is_not_run_on_polls_that_never_needed_it():
    """マーカー判定を事前計算して渡すと、placeholder と初出テキストの2種類の
    ポーリングで走る -- 旧ループが一度も呼んでいなかった場所。呼び出し回数が変わるだけでなく、
    壊れた読み取りに新しい例外経路を与える。"""
    calls = []

    def marker(text):
        calls.append(text)
        return True

    state = S.SettleState()
    state, _ = S.settle_step(state, "処理中です。", now=0.0, dwell_s=1.0, generating=False,
                             is_processing=True, has_marker=marker)
    assert calls == [], "placeholder でマーカー規則が走った"

    state, _ = S.settle_step(state, "初出の本文", now=1.0, dwell_s=1.0, generating=False,
                             is_processing=False, has_marker=marker)
    assert calls == [], "初出テキストでマーカー規則が走った"

    S.settle_step(state, "初出の本文", now=2.0, dwell_s=1.0, generating=False,
                  is_processing=False, has_marker=marker)
    assert calls == ["初出の本文"], "安定判定のときだけ一度走る"


def test_generating_short_circuits_before_any_predicate_runs():
    """生成中は権威ある「まだ続いている」信号。他の述語を評価する理由が無い。"""
    def boom(_text):
        raise AssertionError("生成中に評価された")

    _, outcome = S.settle_step(S.SettleState(), "x", now=0.0, dwell_s=1.0,
                               generating=True, is_processing=boom, has_marker=boom)
    assert outcome == S.RESET


def test_the_canonical_loop_records_a_change_exactly_once():
    """移行時に changed トレースが二重になっていた。collect モードでは1回の変化が
    2行になり、トレースから再構成した履歴が実際の履歴と食い違う。"""
    import io
    src = io.open("relay/copilot_autopilot_relay.py", encoding="utf-8").read()
    i = src.index("state = _settle.SettleState()")
    j = src.index("return False", src.index("_accept_new_reply", i))
    assert src[i:j].count('_settle_trace(self, "changed"') == 1
