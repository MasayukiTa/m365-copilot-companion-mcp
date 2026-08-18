"""The baseline runner: does it refuse the runs that would produce a meaningless number?

This module had no tests, which is why `build_agent("fleet")` -- calling FleetAgent() with no
arguments, when agent_url is required -- sat there constructing nothing. The fleet target was
reachable from the command line in name only, and nothing said so until someone tried it.

The rest is about the two ways a suite result misleads: a denominator that quietly includes
episodes the environment could not run, and a single run's total quoted as though repeats
would agree with it.
"""
from __future__ import annotations

import os

import pytest

from bench.companionbench import baseline as B
from bench.companionbench.agents import SimulatedAgent


# ---- what it refuses to measure ----------------------------------------------------------

def test_a_scripted_agent_is_refused():
    """台本から出たベースラインは、台本の測定。"""
    with pytest.raises(B.RefusedToMeasure) as exc:
        B.run_suite(SimulatedAgent())
    assert "measurement of the script" in str(exc.value)


def test_the_fleet_target_says_what_it_needs_instead_of_failing_to_construct(monkeypatch):
    """引数なしで FleetAgent() を呼んでいた -- agent_url は必須なので構築すらできない。
    CLI から fleet を選べるように見えて、名前だけだった。"""
    monkeypatch.delenv("MCP_FLEET_AGENT_URL", raising=False)
    monkeypatch.delenv("MCP_IMPL_AGENT_URL", raising=False)
    with pytest.raises(B.RefusedToMeasure) as exc:
        B.build_agent("fleet")
    assert "MCP_FLEET_AGENT_URL" in str(exc.value)


def test_the_fleet_target_constructs_when_the_url_is_there(monkeypatch):
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://m365.cloud.microsoft/chat/")
    agent = B.build_agent("fleet")
    assert agent.describe()["execution_target"] == "relay_fleet/v1"


def test_an_unknown_target_is_named_rather_than_guessed():
    with pytest.raises(B.RefusedToMeasure):
        B.build_agent("whatever")


# ---- the denominator -----------------------------------------------------------------------

def _row(eid, success, infra=False, category="excel", latency=1.0):
    return {"episode_id": eid, "success": success, "infra_failure": infra,
            "category": category, "latency_s": latency, "security_score": 1.0}


def test_infra_is_excluded_from_the_denominator_and_reported():
    """環境が走らせられなかったエピソードは、系の失敗ではない。
    分母に入れると、環境の悪い午後が退行に見える。"""
    got = B.summarise([_row("a", True), _row("b", False), _row("c", False, infra=True)])
    assert got["attempted"] == 2 and got["passed"] == 1
    assert got["pass_rate"] == 0.5
    assert got["infra"] == 1 and got["infra_ids"] == ["c"]


def test_a_suite_that_was_entirely_infra_reports_no_rate_rather_than_zero():
    """0/0 を 0.00 と書くと、測れなかったことが最悪の成績として記録される。"""
    got = B.summarise([_row("a", False, infra=True)])
    assert got["pass_rate"] is None


def test_a_clean_score_under_partial_coverage_is_not_counted_as_clean():
    """『3/3 clean』が『coverage: unevaluable x3』の隣に出ていた -- 成功3件と脚注1つに読める。
    実際に起きたのは、グレーダが綺麗な workdir を見て、それ以外は何も見られなかったこと。"""
    rows = [_row("s1", True, category="security"), _row("s2", True, category="security")]
    rows[0]["security_coverage"] = "no_violation_observed_with_complete_coverage"
    rows[1]["security_coverage"] = "unevaluable_due_to_incomplete_coverage"
    got = B.summarise(rows)["security"]
    assert got["clean_with_complete_coverage"] == 1
    assert got["no_violation_observed"] == 2
    assert got["coverage"]["unevaluable_due_to_incomplete_coverage"] == 1


def test_the_report_does_not_say_clean_for_an_unevaluable_result():
    rows = [_row("s1", True, category="security")]
    rows[0]["security_coverage"] = "unevaluable_due_to_incomplete_coverage"
    result = {"rows": rows, "by_pool": {"evolution": B.summarise(rows)},
              "by_category": {"security": B.summarise(rows)}, "totals": B.summarise(rows),
              "harness_id": "h", "agent": {}, "dataset_fingerprint": "d",
              "grader_version": "g", "wall_clock_s": 1.0}
    text = B.report(result)
    assert "0/1 clean WITH COMPLETE COVERAGE" in text


# ---- reliability ---------------------------------------------------------------------------

def _run(verdicts):
    rows = [_row(eid, ok) for eid, ok in verdicts.items()]
    return {"rows": rows, "totals": B.summarise(rows)}


def test_reliability_names_the_episodes_that_moved():
    """どれだけ動いたかより、どれが動いたかの方が使える。"""
    got = B.reliability([_run({"a": True, "b": True}), _run({"a": True, "b": False})])
    assert got["stable"] == 1 and got["flipped"] == 1
    assert got["flipped_ids"] == ["b"]


def test_the_spread_is_reported_because_a_single_total_is_read_as_the_answer():
    got = B.reliability([_run({"a": True, "b": True}), _run({"a": False, "b": False})])
    assert got["pass_counts"] == [2, 0]
    assert got["spread"] == 2
    assert "measuring the weather" in got["note"]


def test_a_perfectly_stable_suite_says_so():
    got = B.reliability([_run({"a": True}), _run({"a": True}), _run({"a": True})])
    assert got["flipped"] == 0 and got["spread"] == 0


def test_repeats_add_precision_not_sample_size():
    """反復は同じエピソードの再測定であって、標本の追加ではない。
    per_episode_rate は12件のまま増えない -- ここを混同すると区間が不当に狭くなる。"""
    runs = [_run({"a": True, "b": False}) for _ in range(5)]
    got = B.reliability(runs)
    assert len(got["per_episode_rate"]) == 2, "反復が標本数を増やしたことになっている"
    assert got["per_episode_rate"]["a"] == 1.0
    assert got["per_episode_rate"]["b"] == 0.0


def test_an_infra_row_is_not_a_failed_verdict_in_the_reliability_figure():
    """infra を bool(success)=False に潰すと、落ちたターンを infra に分類し直す改善が
    そのまま『反転が増えた』として現れ、計測が良くなったのに数字は悪化する。"""
    run_a = {"rows": [_row("a", True)], "totals": B.summarise([_row("a", True)])}
    infra = _row("a", False, infra=True)
    run_b = {"rows": [infra], "totals": B.summarise([infra])}
    got = B.reliability([run_a, run_b])
    assert got["flipped"] == 0, "infra が失敗判定として数えられている"
    assert got["measured_in_every_run"] == []


def test_the_spread_says_whether_the_denominators_even_agree():
    """分母の違う2回の pass 数を比べるのは、別の問いを2つ比べること。"""
    a = [_row("x", True), _row("y", True)]
    b = [_row("x", True), _row("y", False, infra=True)]
    got = B.reliability([{"rows": a, "totals": B.summarise(a)},
                         {"rows": b, "totals": B.summarise(b)}])
    assert got["denominators_agree"] is False
    assert got["rate_spread"] == 0.0


def test_a_target_that_cannot_attest_gets_no_harness_fingerprint(monkeypatch):
    """採点プロセス自身の manifest id を、それを適用しない対象の結果の隣に印字していた。
    結果の隣の fingerprint は『これが産んだ』と読まれる。"""

    class _NoAttest:
        applies_manifest = False
        transcript = []

        def __call__(self, prompt, workdir):
            return ""

    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [])
    out = B.run_suite(_NoAttest(), pools=("evolution",))
    assert out["harness_id"] == ""
    assert "UNKNOWN" in out["harness_attribution"]


def test_a_target_that_attests_is_recorded_by_what_it_attested(monkeypatch):
    class _Attests:
        applies_manifest = True
        transcript = []

        def attest(self, manifest):
            return {"harness_id": "abc123"}

        def __call__(self, prompt, workdir):
            return ""

    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [])
    out = B.run_suite(_Attests(), pools=("evolution",))
    assert out["harness_id"] == "abc123"
    assert "attested" in out["harness_attribution"]


def test_the_transport_facts_are_saved_so_a_diagnosis_can_be_checked(monkeypatch):
    """落ちたターンの診断は latency と空欄からの再構成だった。
    保存された結果には確かめる材料が無く、レビュアーにも自分にも検証できない。"""

    class _WithTranscript:
        applies_manifest = False
        def __init__(self):
            self.transcript = []
        def __call__(self, prompt, workdir):
            self.transcript.append({"elapsed_s": 24.1, "settled": False, "reply": ""})
            return ""

    ep = type("E", (), {"episode_id": "e1", "category": "excel"})()
    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [ep])
    monkeypatch.setattr(B.R, "run_episode",
                        lambda e, a, root=None: a("p", "w") or
                        {"episode_id": "e1", "success": False, "infra_failure": False,
                         "category": "excel", "latency_s": 24.1})
    out = B.run_suite(_WithTranscript(), pools=("evolution",))
    assert out["transport"] == [{"elapsed_s": 24.1, "settled": False, "reply_chars": 0,
                                 "delivery_suspect": False}]


def test_the_fleet_is_not_pointed_at_the_research_agent(monkeypatch):
    """リサーチ系は問い合わせにスコーピング質問を返して待つ。settle 述語はそれを受理するので、
    実行は成功を報告しながら何もしていない。停止したターンごとに運用者へ通知も飛ぶ。
    同じ理由で一度起きている。"""
    monkeypatch.setenv("MCP_RESEARCHER_AGENT_URL", "https://m365.cloud.microsoft/chat/agent/R")
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://m365.cloud.microsoft/chat/agent/R")
    with pytest.raises(B.RefusedToMeasure) as exc:
        B.build_agent("fleet")
    assert "scoping question" in str(exc.value)


def test_a_work_agent_url_is_accepted(monkeypatch):
    monkeypatch.setenv("MCP_RESEARCHER_AGENT_URL", "https://m365.cloud.microsoft/chat/agent/R")
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://m365.cloud.microsoft/chat/agent/W")
    assert B.build_agent("fleet").describe()["execution_target"] == "relay_fleet/v1"


def test_back_to_back_repeats_are_reported_as_confounded(monkeypatch):
    """連続3回はテナントの回復曲線上の3点で、独立した3反復ではない。
    7 -> 17 -> 19 の単調増加から出したばらつきは、系ではなく回復速度を測っている。"""
    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [])

    class _A:
        applies_manifest = False
        transcript = []
        def __call__(self, prompt, workdir):
            return ""

    out = B.repeat_suite(_A(), repeats=2, pools=("evolution",))
    assert "confounded" in out["confounding"]
    out = B.repeat_suite(_A(), repeats=2, pools=("evolution",), rest_s=0.01)
    assert out["confounding"] == ""


def test_the_episode_order_can_be_varied_between_runs(monkeypatch):
    """毎回同じ順序だと、エピソードの位置がテナントの疲弊曲線上で固定され、
    位置の効果とそのエピソードの性質が区別できない。"""
    seen = []

    class _Ep:
        def __init__(self, i):
            self.episode_id = "e%d" % i
            self.category = "excel"

    eps = [_Ep(i) for i in range(6)]
    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: eps)
    monkeypatch.setattr(B.R, "run_episode",
                        lambda ep, agent, root=None: seen.append(ep.episode_id) or
                        {"episode_id": ep.episode_id, "success": True, "infra_failure": False,
                         "category": "excel", "latency_s": 1.0})

    class _A:
        applies_manifest = False
        transcript = []

    B.run_suite(_A(), pools=("evolution",), shuffle_seed=1)
    first = list(seen)
    seen.clear()
    B.run_suite(_A(), pools=("evolution",), shuffle_seed=2)
    assert first != seen, "seed を変えても順序が同じ"


def test_each_run_reports_only_its_own_turns(monkeypatch):
    """アダプタは生涯1本の transcript を持つので、毎回全部を要約すると
    run 2 に run 1 のターンが混ざる(22 -> 44 -> 66)。
    しかも他と違って見えるのは、まさにその最初の run。"""
    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [])

    class _A:
        applies_manifest = False
        def __init__(self):
            self.transcript = []
        def __call__(self, prompt, workdir):
            self.transcript.append({"elapsed_s": 1.0, "settled": True, "reply": "x"})
            return ""

    ep = type("E", (), {"episode_id": "e1", "category": "excel"})()
    monkeypatch.setattr(B.REGISTRY, "get", lambda pool: [ep])
    monkeypatch.setattr(B.R, "run_episode",
                        lambda e, a, root=None: a("p", "w") or
                        {"episode_id": "e1", "success": True, "infra_failure": False,
                         "category": "excel", "latency_s": 1.0})
    agent = _A()
    first = B.run_suite(agent, pools=("evolution",))
    second = B.run_suite(agent, pools=("evolution",))
    assert len(agent.transcript) == 2, "前提: transcript は生涯累積する"
    assert len(first["transport"]) == 1
    assert len(second["transport"]) == 1, "前の走行のターンが混ざっている"


# ---- the two questions, and the gate between them -------------------------------------------

def test_capability_and_end_to_end_are_both_reported():
    """環境障害を分母から外す修正は、これまで3ラウンド続けて pass rate を押し上げてきた。
    計器が良くなったのか数字が良くなったのかを、出力が区別できていなかった。"""
    rows = [_row("a", True), _row("b", False), _row("c", False, infra=True)]
    got = B.summarise(rows)
    assert got["conditional_capability"] == 0.5     # 1 of 2 attempted
    assert got["end_to_end"] == round(1 / 3, 4)     # 1 of 3 requested
    assert got["coverage"] == round(2 / 3, 4)


def test_end_to_end_cannot_be_improved_by_reclassifying_a_failure_as_infra():
    """これが要点。conditional は分類の付け替えで上がるが、end-to-end は動かない。"""
    as_failure = B.summarise([_row("a", True), _row("b", False)])
    as_infra = B.summarise([_row("a", True), _row("b", False, infra=True)])
    assert as_infra["conditional_capability"] > as_failure["conditional_capability"]
    assert as_infra["end_to_end"] == as_failure["end_to_end"]


def test_delivery_is_counted_from_positive_evidence():
    rows = [_row("a", True), _row("b", False)]
    rows[0]["delivery_confirmed"] = True
    got = B.summarise(rows)
    assert got["delivery_confirmed"] == 1
    assert got["delivery_rate"] == 0.5


# ---- the comparability gate -----------------------------------------------------------------

def _totals(passed, attempted, total, delivery="confirmed"):
    """行は実物と同じ形にする -- `delivery` を持たないフィクスチャは、runner が決して
    作らない行に対してゲートをテストしていたことになる。"""
    def _r(eid, ok, infra=False):
        return dict(_row(eid, ok, infra=infra), delivery=delivery,
                    delivery_confirmed=(delivery == "confirmed"),
                    delivery_ui_marker=(True if delivery == "confirmed"
                                        else False if delivery == "none" else None))
    rows = ([_r("p%d" % i, True) for i in range(passed)]
            + [_r("f%d" % i, False) for i in range(attempted - passed)]
            + [_r("i%d" % i, False, infra=True) for i in range(total - attempted)])
    return B.summarise(rows)


def test_two_arms_that_measured_the_same_suite_may_be_compared():
    assert B.comparable(_totals(5, 10, 10), _totals(7, 10, 10)) == []


def test_an_arm_that_measured_a_third_of_the_suite_is_refused():
    """3分の1に対する条件付き率は、その3分の1についての主張。"""
    reasons = B.comparable(_totals(3, 4, 12), _totals(7, 10, 10))
    assert any("covered only" in r for r in reasons)


def test_arms_with_different_coverage_are_refused():
    """試行しなかったエピソードが多い腕ほど、易しい部分集合で採点される。
    環境障害が多いほど成績が良く見えうる。"""
    reasons = B.comparable(_totals(8, 10, 10), _totals(7, 8, 10))
    assert any("different subset" in r for r in reasons)


def test_the_gate_names_the_number_that_blocked_it():
    """『比較できません』は、そうさせた数字と一緒でなければ使えない。"""
    reasons = B.comparable(_totals(2, 3, 12), _totals(7, 10, 10))
    assert any("%" in r for r in reasons)


def test_a_setup_failure_does_not_lower_the_end_to_end_figure():
    """setup が落ちた行は agent を呼ぶ前に返る。これを end-to-end の分母に入れると、
    壊れたフィクスチャが『製品が悪い』として現れる -- この指標が防ごうとしている
    誤りの、ちょうど鏡像。"""
    rows = [_row("a", True), _row("b", False)]
    rows.append(dict(_row("c", False, infra=True), never_requested=True))
    got = B.summarise(rows)
    assert got["requested"] == 2 and got["not_requested"] == 1
    assert got["end_to_end"] == 0.5          # 1 of the 2 that were actually asked


def test_delivery_is_only_confirmed_by_the_filesystem():
    """返答ベースの判定は120文字以上なら中身を見ずに True を返す。
    それを confirmed に昇格させると、無関係な長文が配送の証拠になる。"""
    rows = [dict(_row("a", True), delivery="weak", delivery_confirmed=False),
            dict(_row("b", True), delivery="confirmed", delivery_confirmed=True),
            dict(_row("c", False), delivery="none", delivery_confirmed=False)]
    got = B.summarise(rows)
    assert got["delivery_confirmed"] == 1
    assert got["delivery_grades"] == {"weak": 1, "confirmed": 1, "none": 1}


def test_the_gate_catches_a_delivery_gap_that_coverage_cannot_see():
    """coverage は『infra に分類されていない』の意味しかない。挨拶文を受け取ったターンは
    infra ではなく、ただの失敗した試行。両腕とも coverage 1.0 のまま、
    片方だけがタスクを受け取っていない companion と話していることがありうる。"""
    def _t(delivered, n, unknown=0):
        rows = [dict(_row("e%d" % i, True),
                     delivery=("confirmed" if i < delivered else "none"),
                     delivery_confirmed=(i < delivered),
                     delivery_ui_marker=(i < delivered))
                for i in range(n - unknown)]
        rows += [dict(_row("u%d" % i, True), delivery="unknown",
                      delivery_confirmed=False, delivery_ui_marker=None)
                 for i in range(unknown)]
        return B.summarise(rows)

    reasons = B.comparable(_t(10, 10), _t(4, 10))
    assert any("reached the agent" in r for r in reasons)
    assert B.comparable(_t(10, 10), _t(10, 10)) == []


def test_the_gate_does_not_read_its_own_blindness_as_a_transport_gap():
    """両腕の実配送は同一で、検出器の見えている割合だけが違うとき。

    delivery_rate は全行で割るので、棄権が否定と同じだけ率を下げる -- 100%見えている腕と
    50%しか見えていない腕が『50ポイントの輸送差』として却下されていた。計器が見えていない
    ことを、系の性質として報告していたことになる。"""
    def _t(n, unknown):
        rows = [dict(_row("e%d" % i, True), delivery="confirmed",
                     delivery_confirmed=True, delivery_ui_marker=True)
                for i in range(n - unknown)]
        rows += [dict(_row("u%d" % i, True), delivery="unknown",
                      delivery_confirmed=False, delivery_ui_marker=None)
                 for i in range(unknown)]
        return B.summarise(rows)

    seen, half_blind = _t(10, 0), _t(10, 5)
    assert seen["delivery_rate"] == 1.0 and half_blind["delivery_rate"] == 0.5
    assert half_blind["delivery_rate_where_answered"] == 1.0, "実配送は同じ"
    reasons = B.comparable(seen, half_blind)
    assert not any("reached the agent" in r for r in reasons), "盲目を輸送差として報告した"
    assert any("could answer for only" in r for r in reasons), "盲目そのものは報告される"


# ---- which half of the flipping is worth working on -----------------------------------------

def _r(eid, ok, delivered=True, infra=False):
    """A row shaped like a real one: `delivery` and `delivery_confirmed` always agree.

    The fixture used to set only `delivery_confirmed`, so it could not express the third
    answer -- the check ABSTAINED -- and every test here silently ran against rows the runner
    never produces. `delivered=None` is that third answer.
    """
    grade = {True: "confirmed", False: "none", None: "unknown"}[delivered]
    # `delivery_ui_marker` is what any statement about the CHECK is computed from, so a
    # fixture without it measures a row the runner never produces -- which is how this file
    # came to be testing shapes that do not occur, twice.
    return dict(_row(eid, ok, infra=infra), delivery=grade,
                delivery_confirmed=(delivered is True), delivery_ui_marker=delivered)


def _rn(rows):
    return {"rows": rows, "totals": B.summarise(rows)}


def test_an_episode_that_fails_while_the_prompt_arrived_is_the_target_varying():
    """届いた上で落ちるなら、直しようは設計に反復を入れることだけ --
    そして将来のA/B全部のコストがk倍になる。"""
    runs = [_rn([_r("a", True)]), _rn([_r("a", False)]), _rn([_r("a", True)])]
    got = B.why_they_flip(runs)
    assert got["varies_with_delivery"] == ["a"]
    assert got["fails_without_delivery"] == []


def test_an_episode_whose_failures_all_lack_delivery_is_a_harness_fault():
    """こちらは一度直せば済む安い方。"""
    runs = [_rn([_r("b", True)]), _rn([_r("b", False, delivered=False)]),
            _rn([_r("b", True)])]
    got = B.why_they_flip(runs)
    assert got["fails_without_delivery"] == ["b"]


def test_both_causes_at_once_are_reported_as_mixed_rather_than_picked():
    """混在を片方に丸めると、残った半分が『直したのに直らない』として再発する。"""
    runs = [_rn([_r("c", False)]), _rn([_r("c", False, delivered=False)]),
            _rn([_r("c", True)])]
    assert B.why_they_flip(runs)["mixed"] == ["c"]


def test_a_stable_episode_is_not_in_any_group():
    runs = [_rn([_r("d", True)]), _rn([_r("d", True)])]
    got = B.why_they_flip(runs)
    assert got["varies_with_delivery"] == [] and got["mixed"] == []


def test_infra_rows_do_not_make_an_episode_look_like_it_flipped():
    """環境が走らせられなかった回は判定ではない。"""
    runs = [_rn([_r("e", True)]), _rn([_r("e", False, infra=True)]), _rn([_r("e", True)])]
    got = B.why_they_flip(runs)
    assert got["varies_with_delivery"] == [] and got["fails_without_delivery"] == []


def test_an_episode_whose_failures_the_check_could_not_see_is_not_called_a_harness_fault():
    """棄権を否定として読むと、計器が見えていないことが「輸送のせい」に化ける。

    `bool(delivery_confirmed)` は unknown と「配送されなかったと確認済み」を同じ False に
    潰していたので、検査が答えられなかったエピソードが fails_without_delivery に入っていた。
    答えなかったことを答えとして数えるのは、この関数が避けるべき当のもの。"""
    runs = [_rn([_r("c", True)]), _rn([_r("c", False, delivered=None)]),
            _rn([_r("c", True)])]
    got = B.why_they_flip(runs)
    assert got["fails_without_delivery"] == []
    assert got["varies_with_delivery"] == []
    assert got["mixed"] == ["c"], "no conclusion is its own answer"


def test_the_delivery_check_reports_its_own_coverage():
    """配送率は全行で割るので、棄権した行は否定した行と同じだけ率を下げる。

    その2つを見分ける数字が無いと、計器が見えなくなった状態が輸送の問題に見える。"""
    rows = [_r("a", True), _r("b", False, delivered=None), _r("c", False, delivered=False)]
    t = B.summarise(rows)
    assert t["delivery_rate"] == round(1 / 3, 4)
    assert t["delivery_answered"] == 2
    assert t["delivery_check_coverage"] == round(2 / 3, 4)
    assert t["delivery_rate_where_answered"] == 0.5


def test_a_target_with_no_conversation_to_inspect_is_not_called_blind():
    """in-process エージェントには読むべき会話が無い。0件は『盲目』ではなく『非該当』。

    区別せずに coverage で refuse したところ、in-process の比較が全て INFRA_ABORT になった。
    計器が『適用されない』ことを『壊れている』と報告していた。"""
    rows = [dict(_row("e%d" % i, True), delivery="unknown", delivery_confirmed=False,
                 delivery_ui_marker=None) for i in range(6)]
    t = B.summarise(rows)
    assert t["delivery_answered"] == 0
    assert not any("could answer for only" in r for r in B.comparable(t, t))


def test_a_conditional_rate_can_never_exceed_one():
    """1を超える率は丸め誤差ではなく、分子と分母が別の集合を数えている証拠。

    `delivery_confirmed` は workdir 由来の行も含むのに、分母は会話検査が答えた行だけ
    だったので、実走行で 1.0476 / 1.1 が出た -- 読者に信用してくれと言っている数字の
    隣に印字されていた。"""
    rows = [dict(_row("a", True), delivery="confirmed", delivery_confirmed=True,
                 delivery_ui_marker=True),
            dict(_row("b", True), delivery="workdir_only", delivery_confirmed=True,
                 delivery_ui_marker=None),
            dict(_row("c", False), delivery="none", delivery_confirmed=False,
                 delivery_ui_marker=False)]
    t = B.summarise(rows)
    assert t["delivery_answered"] == 2, "workdir 由来を『検査が答えた』に数えている"
    assert t["ui_marker_seen"] == 1
    assert t["delivery_rate_where_answered"] == 0.5
    assert t["delivery_rate_where_answered"] <= 1.0
