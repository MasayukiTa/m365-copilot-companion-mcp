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
    assert out["transport"] == [{"elapsed_s": 24.1, "settled": False, "reply_chars": 0}]


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
