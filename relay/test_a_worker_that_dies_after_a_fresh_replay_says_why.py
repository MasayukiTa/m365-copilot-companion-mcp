# -*- coding: utf-8 -*-
"""4つの答えのうち2つが、本番で一度も出せなかった。

`review_resilience.diagnose_after_fresh_replay` は4つの答えを返す。そのうち

    SESSION_STATE  同じ依頼が新しい会話では通った
    TASK_CONTENT   同じ依頼が独立した2つの会話で拒否された
    TRANSIENT      新しい会話がインフラ／一過性のエラーで死んだ
    UNKNOWN        手元の証拠では安全な自動回復を特定できない

**下の2つは production で発火し得なかった。** `_apply_diagnosis` の呼び出しは3箇所しか
なく、3箇所とも `fresh_was_transient_error=False` を**リテラルで**渡していた。
その引数を計算するために書かれた `review_resilience.looks_like_transient_error` は、
リポジトリのどこからも呼ばれていない（`tools/unreached.py` の在庫に3件そろって載っている）。

**これは同じファイルで一度直したはずの欠陥の再来。** 2026-09-15 に「2つの答えが settle
パスで直書きされていて、残り2つが到達不能」と診断して直し、`docs/unreached_burndown.md`
には「All four are reachable now」と書いた。**4つ目までは届いていなかった。**
直したのは *enum の答え* の直書きで、*引数* の直書きは残っていた。

## なぜ各 give-up 地点ではなく `_decide` を包んだか

このファイルには `INFRA_STUCK` で worker を終わらせる箇所が**18ある**。18箇所に1行ずつ
足すのは欠陥の言い換えでしかない — 次に足される19個目は、今回の18個が漏れたのと
まったく同じ理由で漏れ、しかも誰も気づかない。`_decide` はその全部が中にある唯一の
漏斗なので、遷移をそこで見る。

## なぜ本文を読み直さないか

`looks_like_transient_error(resp)` を呼べば、**この判断の3つ目の写し**ができる。
relay_fleet は既に5つのマーカー族（transient/agent-dead・tool-unreachable・
canned-nonanswer・admin-block・throttle）で同じことを、しかも `review_resilience` の
`TRANSIENT_MARKERS` と双方向にずれた語彙で判定している。**worker を終わらせた経路は
既に判定を下していて、その結論は `outcome` に入っている。** 同じ文字列を3回目に
突き合わせるより、それを読むほうが強い証拠。
"""
from __future__ import annotations

import pytest

from relay.relay_fleet import RelayWorker


class _Worker:
    """必要な属性だけ持つ器。`RelayWorker` の実体は Playwright の context を要求する
    ので、**診断のメソッドだけを本物のまま**この器に束ねて呼ぶ。"""

    _INFRA_OUTCOMES = RelayWorker._INFRA_OUTCOMES
    _apply_diagnosis = RelayWorker._apply_diagnosis
    _diagnose_terminal_give_up = RelayWorker._diagnose_terminal_give_up

    def __init__(self, **kw):
        self.fresh_replay_count = 1
        self.recovery_cause = ""
        self.recovery_result = ""
        self.reason = ""
        self.status = "stuck"
        self.outcome = "INFRA_STUCK"
        self.transient = 0
        self.__dict__.update(kw)


def _give_up(before="running", **kw):
    w = _Worker(**kw)
    w._diagnose_terminal_give_up(before)
    return w


# ────────────────────────────────────── 到達不能だった2つの答え

def test_an_infra_death_after_a_fresh_replay_is_recorded_as_TRANSIENT():
    """**2番目の答え。** これが本番で出せなかった。"""
    w = _give_up(outcome="INFRA_STUCK")
    assert w.recovery_cause == "transient"
    assert w.recovery_result == "retry_transient"
    assert "transient" in w.reason.lower() or "infrastructure" in w.reason.lower()


def test_a_STUCK_after_transient_retries_were_spent_is_also_TRANSIENT():
    """`transient > 0` は「この失敗は過ぎると信じて使った再試行の回数」— worker 自身の
    カウンタ。使っていたなら、settle させた側は一過性だと扱っていた。"""
    w = _give_up(outcome="STUCK", transient=3)
    assert w.recovery_cause == "transient"


def test_a_STUCK_with_no_transient_retries_is_UNKNOWN_not_transient():
    """**4番目の答え。これも本番で出せなかった。**
    一過性の再試行を1回も使っていない STUCK を TRANSIENT と書けば、settle させた
    コードの判断と矛盾する記録になる。「特定できない」は正しい答えであって、
    空欄の言い換えではない。"""
    w = _give_up(outcome="STUCK", transient=0)
    assert w.recovery_cause == "unknown"
    assert w.recovery_result == "unresolved"


def test_an_ERROR_after_a_fresh_replay_is_also_diagnosed():
    """`error` も TERMINAL。ここが空欄のまま終わるのが、この修正前の姿。"""
    w = _give_up(status="error", outcome="ERROR", transient=0)
    assert w.recovery_cause == "unknown"


# ────────────────────────────────────── 記録してはならない側

def test_a_worker_that_never_replayed_fresh_is_left_alone():
    """新しい会話での再実行が無ければ、この診断が比べているものが存在しない。
    4つの答えはすべて「同じ依頼を2つ目の会話で」についての文。"""
    w = _give_up(fresh_replay_count=0)
    assert w.recovery_cause == ""
    assert w.recovery_result == ""


def test_a_diagnosis_already_made_is_never_overwritten():
    """拒否パスと成功パスは、自分が知っていることで診断している。ここが上書きしたら、
    詳しい答えを弱い答えで潰すことになる。"""
    w = _give_up(recovery_cause="session_state", recovery_result="recovered")
    assert w.recovery_cause == "session_state"
    assert w.recovery_result == "recovered"


def test_a_worker_that_was_already_terminal_is_not_re_diagnosed():
    """`_decide` は再入する（`_resume`）。前のターンで終わった settle は、
    このターンが起こしたものではない。"""
    w = _give_up(before="stuck")
    assert w.recovery_cause == ""


def test_a_worker_still_running_is_not_diagnosed():
    w = _give_up(status="ready", outcome="")
    assert w.recovery_cause == ""


@pytest.mark.parametrize("status,outcome", [("done", "DONE"),
                                            ("content_refused", "CONTENT_REFUSED")])
def test_the_paths_that_own_their_own_diagnosis_are_not_touched(status, outcome):
    """成功と拒否は自分で `_apply_diagnosis` を呼ぶ。ここが先に書いたら、
    どちらが答えたのか分からなくなる。"""
    w = _give_up(status=status, outcome=outcome)
    assert w.recovery_cause == ""


# ────────────────────────────────────── 漏斗そのもの

def test_decide_is_a_wrapper_and_the_real_body_is_reachable_only_through_it():
    """**18箇所に1行ずつではなく1箇所にした、その1箇所が在ること。**
    包みが外れたら、テストは全部緑のまま本番の記録だけが空欄に戻る。"""
    import inspect

    src = inspect.getsource(RelayWorker._decide)
    assert "_decide_impl" in src, "the wrapper no longer delegates to the real body"
    assert "_diagnose_terminal_give_up" in src, "the wrapper no longer diagnoses"
    assert "finally" in src, \
        "the diagnosis must survive the body raising -- it is a record of what happened"


def test_the_funnel_actually_fires_when_the_body_settles_the_worker():
    """ソース断言ではなく実行。**本体を差し替えて `INFRA_STUCK` にさせ、包みが
    診断を書くところまで通す。** 呼ぶのは本物の `_decide`。"""
    class _Fake(_Worker):
        _decide = RelayWorker._decide

        def _decide_impl(self, resp, _resume=False):
            self.status, self.outcome = "stuck", "INFRA_STUCK"
            return "body ran"

    w = _Fake(status="running", outcome="")
    assert w._decide("whatever the agent said") == "body ran"
    assert w.recovery_cause == "transient"


def test_the_body_raising_still_leaves_the_record_written():
    """記録は、それが説明している出来事を失敗させてはならない — そして出来事のほうが
    失敗しても、記録は残っていてほしい。ここが空欄だと、落ちた理由を後から誰も辿れない。"""
    class _Fake(_Worker):
        _decide = RelayWorker._decide

        def _decide_impl(self, resp, _resume=False):
            self.status, self.outcome = "stuck", "INFRA_STUCK"
            raise RuntimeError("the turn blew up after settling")

    w = _Fake(status="running", outcome="")
    with pytest.raises(RuntimeError):
        w._decide("x")
    assert w.recovery_cause == "transient"


def test_a_broken_diagnosis_never_takes_the_turn_down_with_it():
    class _Fake(_Worker):
        _decide = RelayWorker._decide

        def _decide_impl(self, resp, _resume=False):
            return "fine"

        def _diagnose_terminal_give_up(self, before):
            raise RuntimeError("diagnosis is on fire")

    assert _Fake()._decide("x") == "fine"
