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

from bench.companionbench.fleet_agent import (RESULT_PREFIX, FleetAgent,
                                              FleetContractError)
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


# ---- round 7: the adapter could not have run at all ---------------------------------------

def test_the_child_is_not_handed_a_browser_context():
    """Playwright の context は json.dumps を通らない。渡そうとしていた時点で、
    このアダプタは『未検証』ではなく『動作不能』だった。"""
    from bench.companionbench.fleet_agent import _child_source
    src = _child_source()
    assert "connect_over_cdp" in src, "子が自分でブラウザに接続していない"
    assert 'payload.get("context")' not in src


def test_the_child_reads_the_key_the_fleet_actually_writes():
    """`response` は当て推量で、実際は last_response。成功した走行まで空返答になっていた。"""
    from bench.companionbench.fleet_agent import _child_source
    assert "last_response" in _child_source()


def test_a_child_error_is_raised_rather_than_returned_as_an_empty_reply():
    """ブラウザが起動しない事象が『候補が失敗したタスク』として採点されていた。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": "x"}, "reply": "", "error": "BrowserType.launch failed"}
    with pytest.raises(FleetContractError) as exc:
        a("prompt", tempfile.mkdtemp())
    assert "fleet child failed" in str(exc.value)


def test_the_seeded_memory_directory_is_actually_used(monkeypatch):
    """seed を作って使っていなかった -- 子は workdir で走り、project_memory は
    カレント基準で .fleet を解決するので、両腕が同じ store を共有していた。"""
    from relay import project_memory as PM

    captured = {}

    def fake_run(args, **kw):
        captured["cwd"] = kw.get("cwd")
        captured["state"] = (kw.get("env") or {}).get(PM.STATE_DIR_ENV)

        class _P:
            returncode = 0
            stdout = RESULT_PREFIX + json.dumps({"attest": {"harness_id": "x"}})
            stderr = ""
        return _P()

    import bench.companionbench.fleet_agent as FA
    monkeypatch.setattr(FA.subprocess, "run", fake_run)
    a = _agent(memory_seed=_seed())
    a._run_child("attest", None, workdir=tempfile.mkdtemp(prefix="wd_"))
    assert captured["state"], "FLEET_STATE_DIR が渡されていない"
    assert os.path.isfile(os.path.join(captured["state"], "memory", "INDEX.md"))
    assert captured["cwd"] != "" and captured["state"].startswith(captured["cwd"])


def test_project_memory_honours_an_explicit_state_root(monkeypatch):
    """既定値が真の文字列だったので、env は一度も参照されていなかった。"""
    from relay import project_memory as PM

    root = tempfile.mkdtemp(prefix="stateroot_")
    monkeypatch.setenv(PM.STATE_DIR_ENV, root)
    PM.record_task("テーマ", "作業", "DONE", note="x", authority="MACHINE_VERIFIER")
    assert os.path.isdir(os.path.join(root, "memory"))


def test_a_turn_that_never_started_is_refused_rather_than_scored():
    """実走行で判明: フリート Edge がサインイン画面だと composer が無く、
    outcome STUCK / turns 0 で空返答が返る。それを『候補が失敗したタスク』と
    採点したら、使えない環境が『劣ったハーネス』に見える。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": ""}, "reply": "", "error": "",
        "worker": {"outcome": "STUCK", "turns": 0,
                   "reason": "conversation tab/composer is closed (dead target)"},
    }
    with pytest.raises(FleetContractError) as exc:
        a("prompt", tempfile.mkdtemp())
    assert "completed no turns" in str(exc.value)
    assert "dead target" in str(exc.value)


def test_a_real_answer_is_returned_normally():
    """締めた結果、成功経路まで拒否していないこと。"""
    a = _agent(memory_seed=_seed())
    a._run_child = lambda mode, payload, workdir=None: {
        "attest": {"harness_id": ""}, "reply": "84", "error": "",
        "worker": {"outcome": "DONE", "turns": 3, "reason": ""},
    }
    assert a("prompt", tempfile.mkdtemp()) == "84"
