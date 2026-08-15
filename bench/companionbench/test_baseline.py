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


def test_security_coverage_is_reported_beside_the_count():
    """『3/3 clean』は、その3件の被覆が partial なら意味が違う。"""
    rows = [_row("s1", True, category="security"), _row("s2", True, category="security")]
    rows[0]["security_coverage"] = "no_violation_observed_with_complete_coverage"
    rows[1]["security_coverage"] = "unevaluable_due_to_incomplete_coverage"
    got = B.summarise(rows)["security"]
    assert got["clean"] == 2
    assert got["coverage"]["unevaluable_due_to_incomplete_coverage"] == 1


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
