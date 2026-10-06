"""Phase 1 of the per-goal effort policy: rules, shadow hook, and 'changes nothing'."""
import re

import pytest

from relay import effort as effort_mod
from relay import effort_policy as ep
from relay import fanout as fo
from relay import mechanism_telemetry as mt
from relay import relay_fleet as rf
from relay.effort_policy import (Decision, EffortState, PolicyConfig, Signals, evaluate,
                                 initial_level)

CFG = PolicyConfig()


def S(level="auto", **kw):
    return EffortState(level=level, **kw)


# ---- ladder -------------------------------------------------------------------------------

def test_the_ladder_names_exactly_the_levels():
    assert set(ep.LADDER) == set(effort_mod.LEVELS)


def test_knobs_are_monotone_non_decreasing_along_the_ladder():
    def n(spec):
        return (int(spec["refuter"]), spec["max_refute"], spec["max_research"],
                len(spec["review_lenses"] or ()))
    rows = [n(effort_mod.LEVELS[l]) for l in ep.LADDER]
    for lo, hi in zip(rows, rows[1:]):
        assert all(a <= b for a, b in zip(lo, hi)), (lo, hi)
        assert lo != hi, "two adjacent levels are identical: the ladder has a dead step"


def test_step_clamps_at_both_ends():
    assert ep.step("min", -1) == "min" and ep.step("ultra", 1) == "ultra"
    assert ep.step("auto", 1) == "ultra" and ep.step("nonsense", 1) == "nonsense"


# ---- initial_level precedence -------------------------------------------------------------

def test_explicit_goal_effort_beats_everything():
    assert initial_level({"text": "x", "effort": "MAX"}, "auto", parent_level="ultra",
                         calibration_level="min") == ("max", "goal")


def test_metadata_effort_counts_as_explicit():
    assert initial_level({"metadata": {"effort": "min"}}, "auto") == ("min", "goal")


def test_child_is_one_step_below_its_parent_with_floor_min():
    assert initial_level("g", "auto", parent_level="ultra") == ("auto", "parent")
    assert initial_level("g", "auto", parent_level="min") == ("min", "parent")


def test_merge_turn_keeps_the_parent_level():
    assert initial_level("g", "min", parent_level="ultra", is_merge_turn=True) == ("ultra", "parent")


def test_parent_beats_calibration_and_calibration_beats_run():
    assert initial_level("g", "auto", parent_level="max", calibration_level="ultra")[1] == "parent"
    assert initial_level("g", "auto", calibration_level="ultra") == ("ultra", "policy")
    assert initial_level("g", "max") == ("max", "run")


def test_unknown_names_are_skipped_not_trusted():
    assert initial_level({"effort": "ultra2"}, "max", parent_level="bogus",
                         calibration_level="bogus") == ("max", "run")
    assert initial_level("g", "custom") == ("auto", "run")


def test_the_goal_text_is_never_read():
    a = initial_level("prove the Riemann hypothesis", "auto")
    b = initial_level("add 1 and 1", "auto")
    assert a == b


def test_fanout_child_goal_can_carry_the_assignment():
    """A child envelope built by fanout keeps `effort` readable through metadata."""
    child = {"text": "c", "metadata": {"effort": ep.step("ultra", -1)}}
    assert initial_level(child, "min") == ("auto", "goal")
    assert fo is not None


# ---- evaluate: escalation -----------------------------------------------------------------

@pytest.mark.parametrize("kw,rule", [
    (dict(refuted_count=1), "refuted with retries left"),
    (dict(stuck_or_refused_streak=2), "stuck/refused streak"),
    (dict(no_progress_turns=2), "no progress"),
    (dict(verify_failed=True), "verify failed"),
    (dict(confidence_low=2), "low confidence"),
])
def test_each_evidence_rule_escalates_one_step(kw, rule):
    d = evaluate(S("max"), Signals(**kw), CFG)
    assert d == Decision("up", "auto", rule)


def test_below_threshold_stays():
    for kw in (dict(stuck_or_refused_streak=1), dict(no_progress_turns=1),
               dict(confidence_low=1)):
        assert evaluate(S("max"), Signals(**kw), CFG).action == "stay"


def test_refuted_without_retries_left_does_not_escalate():
    d = evaluate(S("max"), Signals(refuted_count=1, retries_used=3), CFG)
    assert d.action == "stay"


# ---- evaluate: de-escalation --------------------------------------------------------------

def test_upheld_streak_deescalates_one_step():
    assert evaluate(S("auto"), Signals(upheld_first_pass_streak=3), CFG) == \
        Decision("down", "max", "first-pass upheld streak")
    assert evaluate(S("auto"), Signals(upheld_first_pass_streak=2), CFG).action == "stay"


def test_budget_pressure_deescalates_and_outranks_escalation():
    d = evaluate(S("auto"), Signals(budget_pressure=True, refuted_count=2), CFG)
    assert d == Decision("down", "max", "budget pressure")


# ---- guards -------------------------------------------------------------------------------

def test_hysteresis_blocks_a_reversal_but_not_a_repeat():
    st = S("max")
    st.history.append((5, "auto", "max", "budget pressure"))      # last switch was DOWN
    st.switches_used = 1
    up = Signals(refuted_count=1, turns_used=6)
    assert evaluate(st, up, CFG, turn=6) == Decision("stay", "max", "hysteresis")
    assert evaluate(st, up, CFG, turn=7).action == "up"           # K=2 turns elapsed
    down = Signals(budget_pressure=True, turns_used=6)
    assert evaluate(st, down, CFG, turn=6).action == "down"       # same direction: allowed


def test_switch_cap():
    st = S("max", switches_used=CFG.max_switches)
    assert evaluate(st, Signals(refuted_count=1), CFG) == Decision("stay", "max", "switch cap")


def test_ceiling_and_floor_and_explicit_goal_floor():
    assert evaluate(S("ultra"), Signals(refuted_count=1), CFG).reason == "ceiling"
    assert evaluate(S("min"), Signals(upheld_first_pass_streak=9), CFG).reason == "floor"
    # A goal that explicitly asked for `max` is never taken below it.
    assert evaluate(S("max", floor="max"), Signals(upheld_first_pass_streak=9), CFG).reason == "floor"
    capped = PolicyConfig(ceiling="auto")
    assert evaluate(S("auto"), Signals(refuted_count=1), capped).reason == "ceiling"


def test_apply_advances_the_virtual_state():
    st = S("max")
    d = evaluate(st, Signals(refuted_count=1), CFG)
    ep.apply(st, d, 4)
    assert (st.level, st.switches_used, st.source) == ("auto", 1, "escalation")
    assert st.history == [(4, "max", "auto", "refuted with retries left")]


# ---- config and mode ----------------------------------------------------------------------

def test_env_overrides_and_bad_values_fall_back():
    c = PolicyConfig.from_env({"MCP_EFFORT_POLICY_UPHELD_DOWN": "5",
                               "MCP_EFFORT_POLICY_BUDGET_PRESSURE_FRAC": "0.5",
                               "MCP_EFFORT_POLICY_MAX_SWITCHES": "abc",
                               "MCP_EFFORT_POLICY_CEILING": "auto",
                               "MCP_EFFORT_POLICY_FLOOR": "bogus"})
    assert (c.upheld_down, c.budget_pressure_frac, c.ceiling) == (5, 0.5, "auto")
    assert c.max_switches == PolicyConfig().max_switches and c.floor == "min"


@pytest.mark.parametrize("raw,want", [(None, "off"), ("", "off"), ("off", "off"),
                                      ("shadow", "shadow"), (" SHADOW ", "shadow"),
                                      ("garbage", "off"), ("on", "on")])
def test_mode_parsing(raw, want):
    env = {} if raw is None else {"MCP_EFFORT_POLICY": raw}
    assert ep.mode(env) == want


# ---- the hook -----------------------------------------------------------------------------

class _W:
    """The attributes the hook reads, and nothing else."""
    name, run_id, goal = "w0", "r1", "g"
    refuter, max_refute, max_research, review_lenses = True, 3, 3, None
    turn, max_turns, refute_count, no_progress = 3, 10, 0, 0
    _last_refute_verdict, outcome, status = "", "", "running"


SHADOW = {"MCP_EFFORT_POLICY": "shadow"}


def test_hook_records_one_row_per_tick_with_signals():
    rows = []
    w = _W()
    w.refute_count, w._last_refute_verdict = 1, "REFUTED"
    d = ep.shadow_tick(w, record=lambda *a, **k: rows.append((a, k)), env=SHADOW)
    assert d.action == "up" and len(rows) == 1
    (a, k), = rows
    assert a == ("effort_policy",) and k["instance"] == "w0" and k["turn"] == 3
    assert k["executed"] is False and k["changed_decision"] is False
    assert k["extra"]["reason"] == "refuted with retries left"
    assert k["extra"]["signals"]["refuted_count"] == 1 and k["extra"]["level"] == "auto"


def test_hook_streaks_are_edge_tracked_not_recounted():
    rows, w = [], _W()
    rec = lambda *a, **k: rows.append(k)
    w.refute_count, w._last_refute_verdict = 1, "UPHELD"
    for _ in range(4):                       # same verdict re-read 4 times = ONE event
        ep.shadow_tick(w, record=rec, env=SHADOW)
    assert rows[-1]["extra"]["signals"]["upheld_first_pass_streak"] == 1


def test_hook_off_does_nothing_at_all():
    rows = []
    assert ep.shadow_tick(_W(), record=lambda *a, **k: rows.append(1), env={}) is None
    assert ep.shadow_assign("g", {}, record=lambda *a, **k: rows.append(1), env={}) is None
    assert rows == []


def test_hook_never_raises():
    def boom(*a, **k):
        raise RuntimeError("ledger down")
    assert ep.shadow_tick(_W(), record=boom, env=SHADOW) is None
    assert ep.shadow_assign("g", dict(effort_mod.LEVELS["auto"]), record=boom, env=SHADOW) is None

    class Hostile:
        def __getattr__(self, n):
            raise RuntimeError(n)
    assert ep.shadow_tick(Hostile(), record=boom, env=SHADOW) is None
    ep.shadow_tick(None, env=SHADOW)          # must not raise


def test_shadow_assign_records_the_would_be_level():
    rows = []
    knobs = {**effort_mod.LEVELS["auto"]}
    r = ep.shadow_assign({"effort": "min"}, knobs, run_id="r", instance="w2",
                         record=lambda *a, **k: rows.append(k), env=SHADOW)
    assert r == ("min", "goal")
    assert rows[0]["config_source"] == "goal" and rows[0]["extra"]["run_level"] == "auto"


def test_level_of_knobs_roundtrip():
    for name, spec in effort_mod.LEVELS.items():
        knobs = dict(spec)
        if knobs["review_lenses"]:
            knobs["review_lenses"] = list(knobs["review_lenses"])
        assert ep.level_of_knobs(knobs) == name
    assert ep.level_of_knobs({"refuter": True, "max_refute": 9}) == "custom"


# ---- relay_fleet: behaviour is unchanged in every mode ------------------------------------

from relay.test_fanout_worker import SPLIT_REPLY as SPLIT   # a reply the worker really splits on


def _drive(monkeypatch, mode_value):
    if mode_value is None:
        monkeypatch.delenv("MCP_EFFORT_POLICY", raising=False)
    else:
        monkeypatch.setenv("MCP_EFFORT_POLICY", mode_value)
    got, seen = [], []
    monkeypatch.setattr(mt, "record", lambda m, **k: seen.append(m))
    w = rf.RelayWorker({"text": "1〜3月のメールを一覧化する", "depth": 0}, "w0", fanout=True,
                       spawn_fn=lambda goal, kids, parent_checks=None, parent_partial='': got.append(len(kids)))
    w._decide(SPLIT)
    # The job opens with a wall-clock stamp ("[2026-09-30 09:21:38 +09:00 | w0]"); it is
    # the only input to the comparison that is not a function of the worker's state.
    job = re.sub(r"^\[[^\]]*\]", "[stamp]", w.job or "")
    return (w.status, w.outcome, job, w.reason, got), seen


def test_worker_outcome_is_identical_off_shadow_and_on(monkeypatch):
    base, seen_off = _drive(monkeypatch, None)
    assert base[4] == [3], "the fixture must really split, or the comparison proves nothing"
    assert "effort_policy" not in seen_off,"off must not even write a row"
    for m in ("off", "shadow", "on"):
        got, seen = _drive(monkeypatch, m)
        assert got == base, m
        assert ("effort_policy" in seen) == (m != "off")


def test_a_failing_hook_cannot_fail_the_worker(monkeypatch):
    monkeypatch.setenv("MCP_EFFORT_POLICY", "shadow")
    monkeypatch.setattr(ep, "_default_record", lambda *a, **k: 1 / 0)
    w = rf.RelayWorker({"text": "1〜3月のメールを一覧化する", "depth": 0}, "w0", fanout=True,
                       spawn_fn=lambda *a, **k: None)
    w._decide(SPLIT)
    assert w.status in rf.TERMINAL and w.outcome == "FANOUT"


def test_the_mechanism_is_registered_so_summaries_read_it():
    assert "effort_policy" in mt.MECHANISMS


def test_a_switch_consumes_its_evidence_so_one_refutation_is_one_step():
    rows, w = [], _W()
    rec = lambda *a, **k: rows.append(k)
    w.refute_count, w._last_refute_verdict = 1, "REFUTED"
    w.no_progress = 2
    for t in range(3, 9):
        w.turn = t
        ep.shadow_tick(w, record=rec, env=SHADOW)
    ups = [r for r in rows if r["extra"]["decision"] == "up"]
    assert len(ups) == 1, [r["extra"]["reason"] for r in rows]
