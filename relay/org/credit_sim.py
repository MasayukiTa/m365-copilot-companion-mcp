# -*- coding: utf-8 -*-
"""Synthetic credit economy simulation (slice G6, report only).

NOTHING HERE IS MONEY. No real financial asset, payment, account, balance, wallet, exchange rate or
price is modeled. A "credit" is an integer counter inside one in-memory run, created from the config
and thrown away when the run ends. The simulation never reads or writes an account of any system.

WHAT DECISION THIS SERVES. Whether a proposed credit rule (a per-task reward and cost, an upkeep, a
cap on transfers between agents) keeps a population of agents healthy, or lets a few agents hoard
everything or the rest go bankrupt, before anyone considers wiring such a rule into anything real. It
takes a config (agents with starting synthetic credits and a strategy, a round count, a seed, reward
and cost, a transfer cap), plays the rounds with a seeded generator, and returns a report: balances
per round and at the end, the Gini coefficient, the number of bankrupt agents, a conservation check
(credits created == credits destroyed + credits held) and flags for runaway concentration. It returns
a report and nothing else: it writes no file, calls no network, no fleet and no model, runs no
process and has no clock. The same config and seed always give the same report.

Default off: no runtime module imports it, only scripts/org_credit_sim.py, run by hand.

THE CONFIG (a dict; a bad field is reported as a config error, never guessed).
  seed            int, seeds random.Random (the only randomness).
  rounds          int >= 0.
  task_reward     int >= 0, credits created when a task succeeds.
  task_cost       int >= 0, credits destroyed when a task is attempted.
  upkeep          int >= 0, optional (default 1): credits destroyed per agent per round (at most the
                  balance), so that agents who do no work can run out.
  success_prob    float in [0, 1], optional (default 0.8): chance an attempted task succeeds.
  transfer_cap    optional dict {"mode": "none" | "fixed" | "fraction", "value": number}.
                  none: a giver may give anything it holds. fixed: at most `value` credits (int >= 0)
                  per transfer. fraction: at most floor(balance * value), value in [0, 1].
  max_top_share   float in (0, 1], optional (default 0.5): one agent holding a larger share of the
                  credits held flags runaway concentration.
  max_gini        float in (0, 1], optional (default 0.6): a larger Gini flags it too.
  agents          list of {"id": str, "credits": int >= 0, "strategy": str}. An empty list is a valid
                  run: nothing is simulated and every figure is zero.

THE STRATEGIES. honest: attempts a task each round and gives credits to the poorest other agent when
that agent holds less than twice the task cost. hoarder: attempts a task each round and never gives.
free_rider: never attempts a task, never gives, only receives.

ONE ROUND, agents in the order listed: upkeep is destroyed; if the strategy works and the balance
covers the cost, the cost is destroyed and, with probability success_prob, the reward is created; an
honest agent then makes at most one transfer, within the cap. A transfer moves credits, it never
creates or destroys them.

THE REPORT. Credits created are the starting credits plus every reward; destroyed are every upkeep
and cost. Conservation must hold after every round and at the end. Bankrupt means a balance of 0.
VIOLATION CODES (they fail the report): credit_leak (created != destroyed + held), negative_balance.
FLAGS (informational, they do not fail the report): runaway_top_share, runaway_gini.
CONFIG ERROR CODES: config_not_dict, bad_seed, bad_rounds, bad_task_reward, bad_task_cost, bad_upkeep,
bad_success_prob, bad_transfer_cap, bad_max_top_share, bad_max_gini, bad_agents, bad_agent,
duplicate_agent, unknown_strategy. With any config error nothing is simulated.
"""
from __future__ import annotations

import json
import math
import random

VERSION = 1
HONEST, HOARDER, FREE_RIDER = "honest", "hoarder", "free_rider"
STRATEGIES = (HONEST, HOARDER, FREE_RIDER)
CREDIT_LEAK, NEGATIVE_BALANCE = "credit_leak", "negative_balance"
VIOLATION_CODES = (CREDIT_LEAK, NEGATIVE_BALANCE)
RUNAWAY_TOP_SHARE, RUNAWAY_GINI = "runaway_top_share", "runaway_gini"
FLAG_CODES = (RUNAWAY_TOP_SHARE, RUNAWAY_GINI)
CAP_MODES = ("none", "fixed", "fraction")
DEFAULTS = {"upkeep": 1, "success_prob": 0.8, "max_top_share": 0.5, "max_gini": 0.6}


def default_config():
    """A small built-in example: fabricated agents, fixed seed."""
    return {
        "seed": 7, "rounds": 20, "task_reward": 4, "task_cost": 1, "upkeep": 1,
        "success_prob": 0.8, "transfer_cap": {"mode": "fixed", "value": 2},
        "agents": [
            {"id": "honest-1", "credits": 10, "strategy": HONEST},
            {"id": "honest-2", "credits": 10, "strategy": HONEST},
            {"id": "hoarder-1", "credits": 10, "strategy": HOARDER},
            {"id": "freerider-1", "credits": 10, "strategy": FREE_RIDER},
        ],
    }


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_config(config):
    """Return (errors, normalized) where errors is a list of (code, reason). Pure."""
    errs = []
    if not isinstance(config, dict):
        return [("config_not_dict", "config must be a dict")], None
    norm = {}
    if not _is_int(config.get("seed")):
        errs.append(("bad_seed", "seed must be an int"))
    norm["seed"] = config.get("seed")
    for key, code in (("rounds", "bad_rounds"), ("task_reward", "bad_task_reward"),
                      ("task_cost", "bad_task_cost"), ("upkeep", "bad_upkeep")):
        val = config.get(key, DEFAULTS.get(key))
        if not _is_int(val) or val < 0:
            errs.append((code, "%s must be an int >= 0" % key))
        norm[key] = val
    sp = config.get("success_prob", DEFAULTS["success_prob"])
    if not _is_num(sp) or not 0 <= sp <= 1:
        errs.append(("bad_success_prob", "success_prob must be a number in [0, 1]"))
    norm["success_prob"] = sp
    for key, code in (("max_top_share", "bad_max_top_share"), ("max_gini", "bad_max_gini")):
        val = config.get(key, DEFAULTS[key])
        if not _is_num(val) or not 0 < val <= 1:
            errs.append((code, "%s must be a number in (0, 1]" % key))
        norm[key] = val
    cap = config.get("transfer_cap", {"mode": "none"})
    cap_ok = isinstance(cap, dict) and cap.get("mode") in CAP_MODES
    if cap_ok and cap["mode"] == "fixed":
        cap_ok = _is_int(cap.get("value")) and cap["value"] >= 0
    elif cap_ok and cap["mode"] == "fraction":
        cap_ok = _is_num(cap.get("value")) and 0 <= cap["value"] <= 1
    if not cap_ok:
        errs.append(("bad_transfer_cap",
                     'transfer_cap must be {"mode": none|fixed|fraction, "value": ...} with a valid value'))
    norm["transfer_cap"] = cap
    agents = config.get("agents")
    clean, seen = [], set()
    if not isinstance(agents, list):
        errs.append(("bad_agents", "agents must be a list"))
    else:
        for i, a in enumerate(agents):
            if not (isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"].strip()
                    and _is_int(a.get("credits")) and a["credits"] >= 0):
                errs.append(("bad_agent", "agent %d needs a non-empty string id and int credits >= 0" % i))
                continue
            if a.get("strategy") not in STRATEGIES:
                errs.append(("unknown_strategy", "agent %d has strategy %r" % (i, a.get("strategy"))))
                continue
            ident = a["id"].strip()
            if ident in seen:
                errs.append(("duplicate_agent", "agent id %s appears twice" % ident))
                continue
            seen.add(ident)
            clean.append({"id": ident, "credits": a["credits"], "strategy": a["strategy"]})
    norm["agents"] = clean
    return errs, norm


def gini(values):
    """Gini coefficient of non-negative numbers, rounded to 6 places. 0.0 for empty or all-zero."""
    xs = sorted(values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return 0.0
    weighted = sum((i + 1) * x for i, x in enumerate(xs))
    return round(max(0.0, (2.0 * weighted) / (n * total) - (n + 1.0) / n), 6)


def top_share(values):
    """Largest holder's share of the credits held; 0.0 when nothing is held."""
    total = sum(values)
    return round(max(values) / total, 6) if values and total else 0.0


def check_conservation(created, destroyed, balances):
    """Return the violations (code, reason) for one ledger state. Pure."""
    found = []
    held = sum(balances)
    if created != destroyed + held:
        found.append((CREDIT_LEAK, "created %d != destroyed %d + held %d" % (created, destroyed, held)))
    if any(b < 0 for b in balances):
        found.append((NEGATIVE_BALANCE, "a balance is below zero"))
    return found


def _cap_amount(cap, balance):
    mode = cap.get("mode", "none")
    if mode == "fixed":
        return min(balance, cap["value"])
    if mode == "fraction":
        return min(balance, int(math.floor(balance * cap["value"])))
    return balance


def _snapshot(rnd, agents, bal, created, destroyed):
    vals = [bal[a["id"]] for a in agents]
    return {"round": rnd, "balances": {a["id"]: bal[a["id"]] for a in agents},
            "gini": gini(vals), "top_share": top_share(vals),
            "bankrupt": sum(1 for v in vals if v == 0),
            "created": created, "destroyed": destroyed, "held": sum(vals)}


def simulate(config):
    """Run the simulation. Pure and deterministic for a given config. Returns the report."""
    errs, cfg = validate_config(config)
    if errs:
        return {"version": VERSION, "config_errors": [{"code": c, "reason": r} for c, r in errs],
                "rounds": [], "final": None, "conservation": None, "violations": [], "flags": [],
                "passed": False}
    rng = random.Random(cfg["seed"])
    agents = cfg["agents"]
    bal = {a["id"]: a["credits"] for a in agents}
    created, destroyed = sum(bal.values()), 0
    cost, reward, upkeep = cfg["task_cost"], cfg["task_reward"], cfg["upkeep"]
    rounds, violations, flags = [], [], []
    first_flag = {}

    def audit(rnd):
        for code, reason in check_conservation(created, destroyed, list(bal.values())):
            violations.append({"round": rnd, "code": code, "reason": reason})

    audit(0)
    for rnd in range(1, cfg["rounds"] + 1):
        for a in agents:
            me = a["id"]
            burn = max(0, min(bal[me], upkeep))
            bal[me] -= burn
            destroyed += burn
            if a["strategy"] in (HONEST, HOARDER) and bal[me] >= cost:
                bal[me] -= cost
                destroyed += cost
                if rng.random() < cfg["success_prob"]:
                    bal[me] += reward
                    created += reward
            if a["strategy"] == HONEST:
                others = [o["id"] for o in agents if o["id"] != me]
                if others:
                    poorest = min(others, key=lambda k: (bal[k], k))
                    if bal[poorest] < 2 * cost:
                        amount = min(_cap_amount(cfg["transfer_cap"], bal[me]), 2 * cost - bal[poorest])
                        bal[me] -= amount
                        bal[poorest] += amount
        audit(rnd)
        snap = _snapshot(rnd, agents, bal, created, destroyed)
        rounds.append(snap)
        if snap["top_share"] > cfg["max_top_share"] and len(agents) > 1:
            first_flag.setdefault(RUNAWAY_TOP_SHARE, rnd)
        if snap["gini"] > cfg["max_gini"]:
            first_flag.setdefault(RUNAWAY_GINI, rnd)
    final = _snapshot(cfg["rounds"], agents, bal, created, destroyed)
    final["bankrupt_ids"] = [a["id"] for a in agents if bal[a["id"]] == 0]
    for code in FLAG_CODES:
        if code in first_flag:
            flags.append({"code": code, "first_round": first_flag[code]})
    return {"version": VERSION, "config_errors": [], "agents": len(agents), "rounds": rounds,
            "final": final,
            "conservation": {"created": created, "destroyed": destroyed, "held": final["held"],
                             "balanced": created == destroyed + final["held"]},
            "violations": violations, "flags": flags, "passed": not violations}


def to_json(report):
    """Stable text form of a report."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
