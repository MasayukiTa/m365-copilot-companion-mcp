"""経路が走行途中で閉じたとき、候補腕は候補腕でなくなる。

危険は fallback 1回のコストではない -- それはターン1回とタブ1枚で、
メモリの数字に既に織り込まれている。値付けされていないのはサーキットブレーカで、
3連続失敗か走行中10回で経路は**一方向に**閉じ、以後のゴールは全部タブになる。
その瞬間から候補腕は対照腕であり、これは今日7つの扉から入ってきた
「両腕が同じプログラム」欠陥の8つ目 -- 走行の途中で発生し、
両腕はその後も普通の数字を報告し続ける。

率の問題ではないので較正は要らない。closed_reason が正確に言う。
"""
import ast
import inspect

import pytest

from relay.selfimprove import compare as C
from relay.selfimprove import route_evaluator as RV
from relay.selfimprove import scheduler as S


def _arm(**kw):
    base = {"done": 4, "goals": 4, "peak_mb": 200.0}
    base.update(kw)
    return base


# ---- 閉じた経路は判定ではなく中断 -----------------------------------------------------------------

def test_a_closed_route_stops_the_verdict():
    got = RV.decide(_arm(peak_mb=600.0),
                    _arm(route_closed_reason="3 consecutive failures, last: token expired"))
    assert got["verdict"] == "inconclusive"
    assert got["aborted"] is True
    assert "same program" in got["why"]


def test_it_does_not_matter_which_arm_closed():
    got = RV.decide(_arm(route_closed_reason="10 fallbacks this run"), _arm())
    assert got.get("aborted") is True
    assert "control arm" in got["why"]


def test_the_closure_is_checked_before_any_number_is_read():
    """閉じた腕のメモリを他方と比べるのは、誰も訊いていない量を測ること。"""
    src = inspect.getsource(RV.decide)
    assert src.index("fallback_verdict") < src.index('done_c = int(')


def test_a_healthy_pair_still_reaches_a_verdict():
    """拒否を足したせいで正常な比較まで潰していないこと。"""
    assert RV.decide(_arm(peak_mb=600.0), _arm(peak_mb=200.0))["verdict"] == "keep"


def test_an_aborted_route_carries_no_memory_number():
    """走行途中で対象が変わったのだから、差分は何の差分でもない。"""
    got = RV.decide(_arm(peak_mb=600.0), _arm(route_closed_reason="x"))
    assert got["memory_gain_mb"] is None


# ---- 規則は1つ、実装も1つ -------------------------------------------------------------------------

def test_the_comparison_calls_the_same_rule_rather_than_restating_it():
    """判定条件が2実装に分かれるのは propose.py の (d) と比較拒否で見た穴と同型。
    片方がずれても、どちらがずれたか誰も言えない。"""
    tree = ast.parse(inspect.getsource(C.decide).lstrip())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert "fallback_verdict" in code
    assert "closed_reason" not in code, "比較側が条件を書き直している"


def test_a_closed_route_in_either_ordering_stops_the_comparison():
    ok = {"memory_gain_mb": 400, "control": _arm(), "candidate": _arm()}
    bad = {"memory_gain_mb": 400, "control": _arm(),
           "candidate": _arm(route_closed_reason="10 fallbacks this run")}
    for pair, which in (((bad, ok), "first"), ((ok, bad), "second")):
        got = C.decide(*pair)
        assert got["verdict"] == C.VERDICT_NONE and got.get("aborted") is True
        assert which in got["why"]


def test_the_judge_rule_lives_in_the_frozen_constitution():
    from relay.selfimprove import frozen as F
    assert "relay/selfimprove/route_evaluator.py" in F.FROZEN_MANIFEST
    assert hasattr(RV, "fallback_verdict")


# ---- 率は記録であって門ではない -------------------------------------------------------------------

def test_the_task_fallback_rate_is_recorded_and_not_gated():
    """22腕88ゴールで観測された task起因 fallback は 0 件。
    測られた基底率が無いところに率の閾値を置けば、それは較正の服を着た数字で、
    メモリの床が帰無走行を得る前に着ていたものと同じ。"""
    got = RV.decide(_arm(peak_mb=600.0), _arm(task_fallbacks=3))
    assert got["verdict"] == "keep", "基底率不明のまま門にしている"
    assert got["task_fallback_rate"] == 0.75
    assert "not gated" in got["why"]


def test_the_reason_the_rate_is_not_a_gate_is_written_down():
    """『まだ門ではない』が読めなければ、次の人が門にする。"""
    src = inspect.getsource(RV)
    i = src.index("TASK_FALLBACK_NOTE_RATE = ")
    block = src[max(0, i - 1200):i]
    assert "no measured baseline" in block
    assert "88 goals" in block or "22 recorded arms" in block


def test_route_caused_fallbacks_are_not_counted_as_task_evidence():
    """トークン失効や切断で学習すると『この時間帯のタスクはタブが要る』を学ぶ。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _task_fallbacks")
    body = src[i:i + 1400]
    assert "classify_fallback" in body
    assert '== "task"' in body


# ---- 規則が読むものを腕が運ぶこと -----------------------------------------------------------------

def test_the_arm_carries_what_the_rule_reads_all_the_way_to_the_verdict():
    """最初の版は `_run` のソースにキーがあることだけを確かめ、緑になった。
    だが `measure_arm` は凍結されていて**固定キーの辞書**を返すので、
    その値は判定に届く前に捨てられていた -- 規則を足したコミットそのものが、
    絶対に通らない分岐を作っていた。検査すべきはホップではなく経路全体。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "out.update(_extras)" in src, "measure_arm が落とす分を腕が拾っていない"

    # measure_arm が実際に落とすことを、推測ではなく実行で確かめる。
    from relay.selfimprove import route_evaluator as RV

    def run_goals(goals, socket_on, sample):
        return {"done": len(goals), "fallbacks": 0,
                "route_closed_reason": "3 consecutive failures", "turns": 9}

    got = RV.measure_arm(run_goals, goals=["a"], socket_on=True, peak_sampler=lambda: 0.0)
    assert "route_closed_reason" not in got, (
        "measure_arm が運ぶようになったなら、この迂回はもう要らない")
    assert "turns" not in got


def test_the_merged_arm_actually_reaches_a_verdict():
    """経路の端から端まで: 腕に載った閉鎖理由が判定を止めること。"""
    arm = {"done": 4, "goals": 4, "peak_mb": 100.0}
    merged = dict(arm, route_closed_reason="10 fallbacks this run")
    assert RV.decide(arm, merged).get("aborted") is True
    assert RV.decide(arm, arm).get("aborted") is not True


def test_the_arm_counts_only_its_own_fallback_rows():
    """両腕が同じログに追記するので、時刻で切らないと前の腕の行を数える。"""
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("def _task_fallbacks")
    assert "_arm_t0[0]" in src[i:i + 1400]


# ---- 規則が読む証拠が、走行中に書き換えられていないこと ---------------------------------------------

def test_the_fallback_log_must_only_have_grown():
    """短くなったログは『fallback が少ない』と読める -- 候補に有利な向き。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert "_evidence_intact" in src
    i = src.index("def _evidence_intact")
    assert "burned_append_only" in src[i:i + 1200], "既存の前置規則を使っていない"


def test_a_rewritten_log_is_an_abort_and_not_a_verdict():
    src = inspect.getsource(S.route_evaluator_for)
    i = src.index("if not _evidence_intact(")
    block = src[i:i + 900]
    assert '"gate": None' in block and '"aborted": True' in block
    assert '"inconclusive"' not in block


def test_the_snapshot_is_taken_before_the_arms_run():
    """腕が走った後に撮ると、比較する相手が結果そのものになる。"""
    src = inspect.getsource(S.route_evaluator_for)
    assert src.index("evidence_before = _evidence_lines(") < src.index("if candidate_first:")


# ---- 活性化の前提: どの世界で測ったか --------------------------------------------------------------

def test_a_comparison_records_the_harness_it_was_taken_under():
    """活性化を入れた日から基底が変わる。記録が無いと、
    活性化前後の行が1つのファイルに混ざり、どちらの世界のものか言えなくなる。
    後から足したフィールドは過去を説明できない。"""
    src = inspect.getsource(C.record)
    assert '"active_harness_id": _active_harness_id()' in src
    assert C._active_harness_id()


# ---- ディスクで塞がれた走行が黙らないこと ----------------------------------------------------------

def test_the_preflight_refuses_below_the_fleets_own_disk_floor():
    """床未満ではアドミッション門が全ワーカーを断り、そのまま sweep を続ける --
    ログ1行も、エラーも、終端状態も無し。較正走行2本が
    status=pending, turn=0 のまま25分ずつ座り、まったく健全に見えた。
    スタックダンプで再現して初めて場所が分かった。"""
    reasons = RV.preflight(free_mb=99999.0, token_ok=True, free_disk_gb=1.0)
    assert any("free on C:" in r for r in reasons), reasons
    assert any("silence" in r for r in reasons), reasons


def test_the_preflight_reads_the_fleets_floor_rather_than_inventing_one():
    """自分の床を持つと、preflight が通した走行をフリートが admit しない
    という食い違いが生まれる -- これはまさにこの検査が防ぐはずの失敗。"""
    from relay import relay_fleet as RF
    assert RV._fleet_disk_floor_gb() == float(RF.DEFAULT_DISK_FLOOR_GB)


def test_plenty_of_disk_is_not_refused():
    assert RV.preflight(free_mb=99999.0, token_ok=True, free_disk_gb=500.0) == []


def test_lowering_the_floor_is_named_as_the_wrong_fix():
    """床を下げれば拒否は消えるが、5つの同時ビルドが C: を潰した事故がそれ。"""
    reasons = RV.preflight(free_mb=99999.0, token_ok=True, free_disk_gb=1.0)
    assert any("do not lower the floor" in r for r in reasons), reasons


def test_the_fleet_says_why_it_is_admitting_nothing(capsys):
    """繰り上げの判断自体は正しい。黙っていたことが欠陥。"""
    from relay import relay_fleet as RF
    RF._DISK_DEFER_LAST[0] = 0.0
    RF._note_disk_defer(6.0, 3)
    out = capsys.readouterr().out
    assert "admitting nothing" in out and "floor" in out and "3 goal" in out


def test_the_notice_does_not_become_the_log(capsys):
    """毎 sweep 出すと、長いドレイン中はログがこの1行で埋まる。"""
    from relay import relay_fleet as RF
    RF._DISK_DEFER_LAST[0] = 0.0
    RF._note_disk_defer(6.0, 3)
    capsys.readouterr()
    RF._note_disk_defer(6.0, 3)
    assert capsys.readouterr().out == ""
