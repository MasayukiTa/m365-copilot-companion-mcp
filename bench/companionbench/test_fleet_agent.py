"""The fleet adapter's refusals and its attestation, without a live fleet.

What is testable here is exactly what the module claims: that a comparison which could not be
attributed to the candidate is REFUSED, and that the child really loads the manifest it was
sent. Driving a browser is not tested and the module says so.

The attestation tests spawn a real child process -- that is the whole point of them. If the
manifest did not survive into the child, they fail, which is the property the five review
rounds were unable to establish any other way.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from bench.companionbench.fleet_agent import FleetAgent, FleetContractError
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC


def _agent(**kw):
    kw.setdefault("agent_url", "http://127.0.0.1:0/unused")
    return FleetAgent(**kw)


def _seed():
    d = tempfile.mkdtemp(prefix="seed_")
    os.makedirs(os.path.join(d, "memory"), exist_ok=True)
    with open(os.path.join(d, "memory", "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("- [テーマ](theme.md)\n")
    return d


def _manifest_file(manifest):
    path = os.path.join(tempfile.mkdtemp(prefix="mf_"), "m.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh)
    return path


# ---- the refusals -------------------------------------------------------------------------

def test_a_field_the_fleet_does_not_read_is_refused():
    base = M.base_manifest()
    # every declared parameter IS read by the fleet, so this uses a component the target
    # cannot reach by pretending the covered set is narrower
    a = _agent(memory_seed=_seed())
    a.covered_fields = frozenset({"parameters.max_retries"})
    cand = M.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    with pytest.raises(FleetContractError) as exc:
        a.check_genome(base, cand)
    assert "same program twice" in str(exc.value)


def test_an_inert_refuter_budget_is_refused():
    """refuter が無効なら max_refute_passes は読まれない。
    読まれないフィールドの A/B は、同じプログラムを2回走らせて p 値を出すこと。"""
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"max_refute_passes": 5}})
    with pytest.raises(FleetContractError) as exc:
        _agent(refuter=False, memory_seed=_seed()).check_genome(base, cand)
    assert "inert" in str(exc.value)

    # with the refuter on it is a real comparison
    _agent(refuter=True, memory_seed=_seed()).check_genome(base, cand)


def test_a_memory_experiment_without_a_seed_is_refused():
    """空の記憶に対する記憶実験は何も動かさない。共有の記憶に対する実験は、
    片方の腕がもう片方の入力を書く。"""
    base = M.base_manifest()
    cand = M.apply_genome(base, {"parameters": {"memory_max_items": 9}})
    with pytest.raises(FleetContractError) as exc:
        _agent().check_genome(base, cand)
    assert "memory_seed" in str(exc.value)


def test_each_arm_gets_its_own_memory_directory():
    """フリートの記憶は走行ごとに読み書きされるので、共有すると対戦が系列になる。"""
    a = _agent(memory_seed=_seed())
    first, _ = a._arm_state_dir()
    second, _ = a._arm_state_dir()
    assert first != second
    assert os.path.isfile(os.path.join(first, ".fleet", "memory", "INDEX.md"))
    assert os.path.isfile(os.path.join(second, ".fleet", "memory", "INDEX.md"))


def test_the_child_is_not_given_the_parameters_under_test():
    """run_relay_fleet は明示引数を manifest より優先する -- 意図的に。
    だからここで渡すと、試験対象のフィールドを自分で黙らせることになる。"""
    from bench.companionbench.fleet_agent import _child_source
    src = _child_source()
    assert "max_transient" not in src.split("run_relay_fleet")[1].split(")")[0]
    assert "max_refute=" not in src


# ---- the attestation: a real child, carrying a real manifest -------------------------------

def test_the_child_loads_the_manifest_it_was_sent(monkeypatch):
    """5ラウンドかけても他の方法では確立できなかった性質: manifest が実際に
    実行側へ届いていること。子プロセスに聞き、返答を検証する。"""
    cand = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 11}})
    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(cand))
    got = _agent(memory_seed=_seed()).attest(cand)
    assert got["harness_id"] == M.harness_id(cand), "子プロセスに manifest が届いていない"
    assert got["execution_target"] == "relay_fleet/v1"
    assert got["effective"]["memory_max_items"] == 11


def test_a_different_manifest_produces_a_different_attestation(monkeypatch):
    """同じ値を返し続けるだけの attest は検査になっていない。"""
    a = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 2}})
    b = M.apply_genome(M.base_manifest(), {"parameters": {"memory_max_items": 12}})
    agent = _agent(memory_seed=_seed())

    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(a))
    got_a = agent.attest(a)
    monkeypatch.setenv(RC.OVERRIDE_ENV, _manifest_file(b))
    got_b = agent.attest(b)

    assert got_a["harness_id"] != got_b["harness_id"]
    assert got_a["effective"]["memory_max_items"] == 2
    assert got_b["effective"]["memory_max_items"] == 12


def test_the_adapter_satisfies_the_runner_contract():
    from bench.companionbench import agents as A
    assert FleetAgent.execution_target == A.FLEET
    assert "parameters.max_retries" in FleetAgent.covered_fields
    assert hasattr(FleetAgent, "attest")
