# -*- coding: utf-8 -*-
"""Synthetic credit economy simulation (slice G6). Synthetic data only; no real money."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import credit_sim as C  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_credit_sim.py")


def cfg(**over):
    c = C.default_config()
    c.update(over)
    return c


def agent(i, credits, strategy):
    return {"id": i, "credits": credits, "strategy": strategy}


def err_codes(rep):
    return [e["code"] for e in rep["config_errors"]]


def test_default_config_runs_and_passes():
    rep = C.simulate(C.default_config())
    assert rep["passed"] is True and rep["config_errors"] == []
    assert len(rep["rounds"]) == 20 and rep["agents"] == 4


@pytest.mark.parametrize("seed", [0, 1, 7, 12345])
def test_conservation_every_round(seed):
    rep = C.simulate(cfg(seed=seed, rounds=60))
    for r in rep["rounds"]:
        assert r["created"] == r["destroyed"] + r["held"]
        assert sum(r["balances"].values()) == r["held"]
        assert all(v >= 0 for v in r["balances"].values())
    c = rep["conservation"]
    assert c["balanced"] is True and c["created"] == c["destroyed"] + c["held"]
    assert rep["violations"] == []


def test_conservation_with_each_cap_mode():
    for cap in ({"mode": "none"}, {"mode": "fixed", "value": 0}, {"mode": "fixed", "value": 5},
                {"mode": "fraction", "value": 0.0}, {"mode": "fraction", "value": 0.5},
                {"mode": "fraction", "value": 1}):
        rep = C.simulate(cfg(transfer_cap=cap, rounds=40))
        assert rep["passed"] is True and rep["conservation"]["balanced"] is True


def test_deterministic_same_seed():
    a = C.simulate(cfg(seed=99, rounds=50))
    b = C.simulate(cfg(seed=99, rounds=50))
    assert a == b and C.to_json(a) == C.to_json(b)


def test_config_not_mutated():
    c = cfg(seed=3)
    before = copy.deepcopy(c)
    C.simulate(c)
    assert c == before


def test_different_seeds_differ():
    a = C.simulate(cfg(seed=1, rounds=50))["final"]["balances"]
    b = C.simulate(cfg(seed=2, rounds=50))["final"]["balances"]
    assert a != b


def test_zero_rounds_keeps_starting_balances():
    rep = C.simulate(cfg(rounds=0))
    assert rep["rounds"] == []
    assert rep["final"]["balances"] == {"honest-1": 10, "honest-2": 10, "hoarder-1": 10, "freerider-1": 10}
    assert rep["conservation"] == {"created": 40, "destroyed": 0, "held": 40, "balanced": True}


def test_free_rider_goes_bankrupt_from_upkeep():
    rep = C.simulate(cfg(agents=[agent("f", 3, C.FREE_RIDER)], rounds=5, upkeep=1))
    assert rep["final"]["bankrupt"] == 1 and rep["final"]["bankrupt_ids"] == ["f"]
    assert rep["final"]["balances"] == {"f": 0}
    assert rep["conservation"]["destroyed"] == 3 and rep["passed"] is True
    assert [r["bankrupt"] for r in rep["rounds"]] == [0, 0, 1, 1, 1]


def test_no_bankruptcy_when_nothing_destroyed():
    rep = C.simulate(cfg(agents=[agent("a", 5, C.HONEST), agent("b", 5, C.FREE_RIDER)],
                         upkeep=0, task_cost=0, rounds=10))
    assert rep["final"]["bankrupt"] == 0 and rep["final"]["bankrupt_ids"] == []


def test_unaffordable_task_is_skipped_not_negative():
    rep = C.simulate(cfg(agents=[agent("h", 1, C.HOARDER)], task_cost=5, upkeep=0, rounds=10))
    assert rep["final"]["balances"] == {"h": 1} and rep["conservation"]["destroyed"] == 0


def test_certain_success_pays_reward_minus_cost():
    rep = C.simulate(cfg(agents=[agent("h", 10, C.HOARDER)], task_cost=2, task_reward=5,
                         upkeep=0, success_prob=1.0, rounds=4))
    assert rep["final"]["balances"] == {"h": 22}
    assert rep["conservation"] == {"created": 30, "destroyed": 8, "held": 22, "balanced": True}


def test_zero_success_prob_only_burns():
    rep = C.simulate(cfg(agents=[agent("h", 10, C.HOARDER)], task_cost=2, upkeep=0,
                         success_prob=0, rounds=3))
    assert rep["final"]["balances"] == {"h": 4}


def test_honest_transfers_to_poorest_within_fixed_cap():
    rep = C.simulate(cfg(agents=[agent("h", 20, C.HONEST), agent("p", 0, C.FREE_RIDER)],
                         task_cost=2, upkeep=0, success_prob=0, rounds=1,
                         transfer_cap={"mode": "fixed", "value": 3}))
    assert rep["final"]["balances"] == {"h": 15, "p": 3}


def test_zero_cap_blocks_transfers():
    rep = C.simulate(cfg(agents=[agent("h", 20, C.HONEST), agent("p", 0, C.FREE_RIDER)],
                         task_cost=2, upkeep=0, success_prob=0, rounds=3,
                         transfer_cap={"mode": "fixed", "value": 0}))
    assert rep["final"]["balances"]["p"] == 0


def test_fraction_cap_floors():
    rep = C.simulate(cfg(agents=[agent("h", 21, C.HONEST), agent("p", 0, C.FREE_RIDER)],
                         task_cost=1, upkeep=0, success_prob=0, rounds=1,
                         transfer_cap={"mode": "fraction", "value": 0.1}))
    assert rep["final"]["balances"] == {"h": 18, "p": 2}


def test_hoarder_never_gives():
    rep = C.simulate(cfg(agents=[agent("h", 50, C.HOARDER), agent("p", 0, C.FREE_RIDER)],
                         upkeep=0, rounds=10))
    assert rep["final"]["balances"]["p"] == 0


def test_runaway_concentration_flagged():
    rep = C.simulate(cfg(agents=[agent("whale", 1000, C.HOARDER), agent("a", 1, C.FREE_RIDER),
                                 agent("b", 1, C.FREE_RIDER)], rounds=3))
    codes = {f["code"]: f["first_round"] for f in rep["flags"]}
    assert codes[C.RUNAWAY_TOP_SHARE] == 1 and codes[C.RUNAWAY_GINI] == 1
    assert rep["passed"] is True  # flags are informational


def test_no_flag_for_equal_holders():
    rep = C.simulate(cfg(agents=[agent("a", 10, C.HOARDER), agent("b", 10, C.HOARDER)],
                         task_cost=0, task_reward=0, upkeep=0, rounds=3))
    assert rep["flags"] == [] and rep["final"]["gini"] == 0.0 and rep["final"]["top_share"] == 0.5


def test_single_agent_never_flags_top_share():
    rep = C.simulate(cfg(agents=[agent("solo", 10, C.HOARDER)], rounds=3))
    assert all(f["code"] != C.RUNAWAY_TOP_SHARE for f in rep["flags"])


def test_thresholds_are_configurable():
    base = dict(agents=[agent("a", 6, C.HOARDER), agent("b", 4, C.HOARDER)],
                task_cost=0, task_reward=0, upkeep=0, rounds=1)
    assert [f["code"] for f in C.simulate(cfg(**base))["flags"]] == [C.RUNAWAY_TOP_SHARE]
    assert C.simulate(cfg(max_top_share=0.7, **base))["flags"] == []


def test_gini_values():
    assert C.gini([]) == 0.0 and C.gini([0, 0]) == 0.0 and C.gini([5, 5, 5]) == 0.0
    assert C.gini([0, 0, 0, 10]) == 0.75
    assert C.gini([1, 2, 3, 4]) == 0.25
    assert C.gini([4, 1, 3, 2]) == C.gini([1, 2, 3, 4])


def test_top_share_values():
    assert C.top_share([]) == 0.0 and C.top_share([0, 0]) == 0.0
    assert C.top_share([1, 3]) == 0.75


def test_leak_is_a_violation():
    assert C.check_conservation(10, 3, [4, 3]) == []
    leak = C.check_conservation(10, 3, [4, 4])
    assert [c for c, _ in leak] == [C.CREDIT_LEAK]
    lost = C.check_conservation(10, 3, [4, 2])
    assert [c for c, _ in lost] == [C.CREDIT_LEAK]


def test_negative_balance_is_a_violation():
    codes = [c for c, _ in C.check_conservation(13, 3, [11, -1])]
    assert codes == [C.NEGATIVE_BALANCE]


def test_corrupt_transfer_amount_fails_report(monkeypatch):
    monkeypatch.setattr(C, "_cap_amount", lambda cap, balance: -5)
    rep = C.simulate(cfg(agents=[agent("h", 20, C.HONEST), agent("p", 0, C.FREE_RIDER)],
                         task_cost=2, upkeep=0, rounds=2))
    assert rep["passed"] is False
    assert C.NEGATIVE_BALANCE in {v["code"] for v in rep["violations"]}


def test_empty_agents_is_a_valid_empty_run():
    rep = C.simulate(cfg(agents=[], rounds=3))
    assert rep["passed"] is True and rep["agents"] == 0 and len(rep["rounds"]) == 3
    assert rep["final"]["gini"] == 0.0 and rep["final"]["bankrupt"] == 0 and rep["final"]["balances"] == {}
    assert rep["conservation"] == {"created": 0, "destroyed": 0, "held": 0, "balanced": True}
    assert rep["flags"] == []


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_config_not_dict(bad):
    rep = C.simulate(bad)
    assert err_codes(rep) == ["config_not_dict"] and rep["passed"] is False and rep["rounds"] == []


@pytest.mark.parametrize("key,value,code", [
    ("seed", "1", "bad_seed"), ("seed", 1.5, "bad_seed"), ("seed", True, "bad_seed"),
    ("rounds", -1, "bad_rounds"), ("rounds", 2.0, "bad_rounds"),
    ("task_reward", -1, "bad_task_reward"), ("task_cost", "2", "bad_task_cost"),
    ("upkeep", -3, "bad_upkeep"), ("success_prob", 1.5, "bad_success_prob"),
    ("success_prob", float("nan"), "bad_success_prob"), ("success_prob", True, "bad_success_prob"),
    ("max_top_share", 0, "bad_max_top_share"), ("max_gini", 2, "bad_max_gini"),
    ("agents", "x", "bad_agents"), ("agents", None, "bad_agents"),
    ("transfer_cap", "fixed", "bad_transfer_cap"),
    ("transfer_cap", {"mode": "weird"}, "bad_transfer_cap"),
    ("transfer_cap", {"mode": "fixed"}, "bad_transfer_cap"),
    ("transfer_cap", {"mode": "fixed", "value": -1}, "bad_transfer_cap"),
    ("transfer_cap", {"mode": "fraction", "value": 1.5}, "bad_transfer_cap"),
])
def test_malformed_config_fields(key, value, code):
    rep = C.simulate(cfg(**{key: value}))
    assert code in err_codes(rep) and rep["passed"] is False
    assert rep["final"] is None and rep["rounds"] == []


def test_missing_seed_is_an_error():
    c = C.default_config()
    del c["seed"]
    assert "bad_seed" in err_codes(C.simulate(c))


@pytest.mark.parametrize("bad,code", [
    ("notadict", "bad_agent"), ({"id": "", "credits": 1, "strategy": "honest"}, "bad_agent"),
    ({"id": "a", "credits": -1, "strategy": "honest"}, "bad_agent"),
    ({"id": "a", "credits": 1.5, "strategy": "honest"}, "bad_agent"),
    ({"id": "a", "credits": 1, "strategy": "villain"}, "unknown_strategy"),
    ({"id": "a", "credits": 1}, "unknown_strategy"),
])
def test_malformed_agent(bad, code):
    assert code in err_codes(C.simulate(cfg(agents=[bad])))


def test_duplicate_agent_id():
    rep = C.simulate(cfg(agents=[agent("a", 1, C.HONEST), agent(" a ", 1, C.HOARDER)]))
    assert err_codes(rep) == ["duplicate_agent"]


def test_all_errors_collected_together():
    rep = C.simulate({"seed": "x", "rounds": -1, "agents": "no"})
    assert {"bad_seed", "bad_rounds", "bad_agents"} <= set(err_codes(rep))


def test_defaults_applied_when_optional_fields_missing():
    rep = C.simulate({"seed": 1, "rounds": 2, "task_reward": 3, "task_cost": 2,
                      "agents": [agent("a", 5, C.HONEST)]})
    assert rep["passed"] is True and len(rep["rounds"]) == 2


def run_cli(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_cli_default_text():
    p = run_cli()
    assert p.returncode == 0
    assert "PASS" in p.stdout and "no real money" in p.stdout and "balanced=True" in p.stdout


def test_cli_json_matches_module(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cfg(seed=5, rounds=8)), encoding="utf-8")
    p = run_cli("--config", str(f), "--format", "json")
    assert p.returncode == 0
    assert json.loads(p.stdout) == json.loads(C.to_json(C.simulate(cfg(seed=5, rounds=8))))


def test_cli_config_error_exits_1(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"seed": "x"}), encoding="utf-8")
    p = run_cli("--config", str(f))
    assert p.returncode == 1 and "config error" in p.stdout


def test_cli_refuses_unreadable_and_non_json(tmp_path):
    assert run_cli("--config", str(tmp_path / "missing.json")).returncode == 2
    f = tmp_path / "x.json"
    f.write_text("not json", encoding="utf-8")
    assert run_cli("--config", str(f)).returncode == 2


def test_cli_writes_no_file(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cfg()), encoding="utf-8")
    run_cli("--config", str(f))
    assert sorted(os.listdir(tmp_path)) == ["c.json"]


def test_module_has_no_runtime_importer():
    needle = "credit_sim"
    for root in ("relay", "scripts"):
        for dirpath, _, files in os.walk(os.path.join(REPO, root)):
            for name in files:
                if not name.endswith(".py") or name in ("credit_sim.py", "org_credit_sim.py"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                    assert needle not in fh.read(), os.path.join(dirpath, name)
