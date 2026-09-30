"""A per-goal effort POLICY: when should one goal's effort change, on what evidence.

PHASE 1 OF 5, SHADOW ONLY. Nothing in this module changes what a worker does. With
MCP_EFFORT_POLICY=shadow it records, per turn, the decision it WOULD have taken; with the
default (off) it does nothing at all. `on` is reserved and, until phase 3, behaves exactly
like shadow. See docs/private/20260930_effort_policy_design.md.

THE REPOSITORY RULE THIS FOLLOWS. Difficulty is never inferred from a goal's wording. Effort
is set from (a) something that KNEW -- an explicit goal effort, the parent goal, a measured
calibration -- and (b) in-run evidence the worker itself produced: a refuter verdict, a stuck
streak, no progress, a failed verification. Nothing here reads goal text.

PURE. No I/O except through injected callables (`record`, `log`). That is what lets the rule
be replayed offline over a recorded ledger (scripts/effort_policy_replay.py) with the SAME
function the worker calls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

from . import effort as effort_mod

#: Ordinal ladder, cheapest to most thorough. Derived from effort.LEVELS rather than typed a
#: second time: test_effort_policy asserts the knobs are monotone non-decreasing along it, so
#: a level added to LEVELS in the wrong place fails a test instead of silently mis-ordering.
LADDER = ("min", "max", "auto", "ultra")


def rank(level):
    """Position on the ladder, or -1 for a name that is not on it."""
    try:
        return LADDER.index(level)
    except ValueError:
        return -1


def step(level, delta):
    """`delta` steps from `level`, clamped to the ladder ends. Unknown level -> unchanged."""
    i = rank(level)
    if i < 0:
        return level
    return LADDER[max(0, min(len(LADDER) - 1, i + delta))]


# ---------------------------------------------------------------------------------------
# configuration: every threshold in one place, each overridable as MCP_EFFORT_POLICY_<NAME>
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicyConfig:
    #: escalate when at least this many REFUTED verdicts have been seen and retries remain.
    refuted_up: int = 1
    #: retries a goal may spend before a REFUTED no longer justifies escalating.
    max_retries: int = 3
    #: escalate after this many consecutive STUCK/REFUSED turns.
    stuck_streak_up: int = 2
    #: escalate after this many consecutive no-progress (repeated reply) turns.
    no_progress_up: int = 2
    #: escalate when the last verification failed.
    verify_failed_up: bool = True
    #: escalate after this many consecutive low-confidence turns.
    confidence_low_up: int = 2
    #: de-escalate after this many consecutive first-pass UPHELD verdicts.
    upheld_down: int = 3
    #: fraction of the turn budget after which budget pressure de-escalates.
    budget_pressure_frac: float = 0.8
    #: no reversal of direction within this many turns of the previous switch.
    hysteresis_turns: int = 2
    #: at most this many switches per goal.
    max_switches: int = 3
    #: hard bounds for the policy (a goal's explicit effort raises `floor` per goal).
    floor: str = "min"
    ceiling: str = "ultra"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            raw = env.get("MCP_EFFORT_POLICY_" + f.name.upper())
            if raw is None or not str(raw).strip():
                continue
            raw = str(raw).strip()
            try:
                if f.type in ("int", int):
                    kw[f.name] = int(raw)
                elif f.type in ("float", float):
                    kw[f.name] = float(raw)
                elif f.type in ("bool", bool):
                    kw[f.name] = raw.lower() in ("1", "true", "yes", "on")
                else:
                    if raw.lower() in LADDER:
                        kw[f.name] = raw.lower()
            except ValueError:
                pass            # a malformed override keeps the documented default
        return cls(**kw)


# ---------------------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------------------
MODES = ("off", "shadow", "on")
_warned_on = [False]


def mode(env=None, log=None):
    """MCP_EFFORT_POLICY = off|shadow|on, default off. Unknown values mean off.

    `on` is NOT implemented in phase 1: it is treated exactly as shadow, and the fact is
    logged once per process so nobody believes the policy is steering.
    """
    env = os.environ if env is None else env
    raw = str(env.get("MCP_EFFORT_POLICY", "") or "").strip().lower()
    if raw not in MODES:
        return "off"
    if raw == "on":
        if not _warned_on[0]:
            _warned_on[0] = True
            if log:
                try:
                    log("[effort_policy] MCP_EFFORT_POLICY=on is not active yet "
                        "(phase 1): running as shadow, behaviour unchanged")
                except Exception:
                    pass
        return "shadow"
    return raw


# ---------------------------------------------------------------------------------------
# state and initial assignment
# ---------------------------------------------------------------------------------------
@dataclass
class EffortState:
    level: str
    source: str = "run"          # run | goal | parent | policy | escalation
    floor: str = "min"           # per-goal floor; raised to the explicit goal effort
    history: list = field(default_factory=list)   # (turn, from, to, reason)
    switches_used: int = 0


def initial_level(goal, run_level, *, parent_level=None, is_merge_turn=False,
                  calibration_level=None):
    """The level a goal STARTS at, and where that came from. Returns (level, source).

    Precedence: explicit goal effort > parent-derived > calibration > run level.
    A fan-out child sits ONE STEP BELOW its parent (floor min); the merge/verify turn of a
    parent keeps the parent's level, because it judges the children's combined work.
    Unknown names at any layer are skipped, not trusted.
    """
    explicit = effort_mod.goal_effort(goal)
    if explicit in LADDER:
        return explicit, "goal"
    if parent_level in LADDER:
        if is_merge_turn:
            return parent_level, "parent"
        return step(parent_level, -1), "parent"
    if calibration_level in LADDER:
        return calibration_level, "policy"
    return (run_level if run_level in LADDER else "auto"), "run"


# ---------------------------------------------------------------------------------------
# evidence and decision
# ---------------------------------------------------------------------------------------
@dataclass
class Signals:
    refuted_count: int = 0
    upheld_first_pass_streak: int = 0
    stuck_or_refused_streak: int = 0
    no_progress_turns: int = 0
    retries_used: int = 0
    verify_failed: bool = False
    #: consecutive turns with low confidence. No live source exists yet (phase 1), so the
    #: hook passes 0; the rule is implemented and tested for when one does.
    confidence_low: int = 0
    turns_used: int = 0
    turn_budget: int = 0
    budget_pressure: bool = False

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Decision:
    action: str            # stay | up | down
    target_level: str
    reason: str            # a rule name, or why it stayed


def _bounds(state, cfg):
    lo = max(rank(cfg.floor), rank(state.floor), 0)
    hi = rank(cfg.ceiling) if rank(cfg.ceiling) >= 0 else len(LADDER) - 1
    return lo, hi


def evaluate(state, signals, cfg=None, *, turn=None):
    """One step of policy. Escalation is one step on evidence; de-escalation one step on
    sustained success or budget pressure. Budget pressure outranks every escalation rule:
    escalating spends what is already scarce.

    GUARDS, in order: switch cap, hysteresis (no REVERSAL within cfg.hysteresis_turns of the
    last switch), floor/ceiling. A guard turns the decision into `stay` and names itself.
    """
    cfg = cfg or PolicyConfig()
    turn = signals.turns_used if turn is None else turn
    lo, hi = _bounds(state, cfg)
    cur = rank(state.level)
    if cur < 0:
        return Decision("stay", state.level, "unknown level")

    action, rule = "stay", "no evidence"
    if signals.budget_pressure:
        action, rule = "down", "budget pressure"
    elif signals.refuted_count >= cfg.refuted_up and signals.retries_used < cfg.max_retries:
        action, rule = "up", "refuted with retries left"
    elif signals.stuck_or_refused_streak >= cfg.stuck_streak_up:
        action, rule = "up", "stuck/refused streak"
    elif signals.no_progress_turns >= cfg.no_progress_up:
        action, rule = "up", "no progress"
    elif cfg.verify_failed_up and signals.verify_failed:
        action, rule = "up", "verify failed"
    elif signals.confidence_low >= cfg.confidence_low_up:
        action, rule = "up", "low confidence"
    elif signals.upheld_first_pass_streak >= cfg.upheld_down:
        action, rule = "down", "first-pass upheld streak"

    if action == "stay":
        return Decision("stay", state.level, rule)
    if state.switches_used >= cfg.max_switches:
        return Decision("stay", state.level, "switch cap")
    if state.history:
        last_turn, last_from, last_to, _ = state.history[-1]
        reversal = (rank(last_to) - rank(last_from)) * (1 if action == "up" else -1) < 0
        if reversal and (turn - last_turn) < cfg.hysteresis_turns:
            return Decision("stay", state.level, "hysteresis")
    target = cur + (1 if action == "up" else -1)
    if target > hi:
        return Decision("stay", state.level, "ceiling")
    if target < lo:
        return Decision("stay", state.level, "floor")
    return Decision(action, LADDER[target], rule)


def apply(state, decision, turn):
    """Advance the VIRTUAL state in shadow mode (nothing real is changed by this)."""
    if decision.action == "stay":
        return state
    state.history.append((turn, state.level, decision.target_level, decision.reason))
    state.level = decision.target_level
    state.switches_used += 1
    state.source = "escalation" if decision.action == "up" else "policy"
    return state


# ---------------------------------------------------------------------------------------
# shadow hooks (the only functions relay_fleet calls)
# ---------------------------------------------------------------------------------------
_TROUBLE_OUTCOMES = ("STUCK", "INFRA_STUCK", "REFUSED", "CONTENT_REFUSED")


def level_of_knobs(knobs):
    """Name the ladder level whose knobs equal `knobs`, else "custom"."""
    try:
        for name in LADDER:
            spec = effort_mod.LEVELS[name]
            if all((list(spec[k]) if isinstance(spec[k], tuple) else spec[k])
                   == (list(knobs.get(k)) if isinstance(knobs.get(k), (list, tuple))
                       else knobs.get(k)) for k in effort_mod.KNOBS):
                return name
    except Exception:
        pass
    return "custom"


def _default_record(*a, **kw):
    from . import mechanism_telemetry as mt
    return mt.record(*a, **kw)


def shadow_assign(goal, knobs, *, run_id="", instance="", record=None, log=None,
                  env=None, **initial_kw):
    """Record the would-be initial assignment at worker creation. Never raises."""
    try:
        if mode(env, log) == "off":
            return None
        run_level = level_of_knobs(knobs or {})
        level, source = initial_level(goal, run_level if run_level != "custom" else "auto",
                                      **initial_kw)
        (record or _default_record)(
            "effort_policy", run_id=run_id, instance=instance, turn=0,
            configured=True, config_source=source, config_value=level,
            eligible=True, triggered=False, executed=False, changed_decision=False,
            before=run_level, after=level,
            extra={"event": "initial", "run_level": run_level, "mode": "shadow"})
        return level, source
    except Exception:
        return None


def _shadow_state(worker, level, floor):
    st = getattr(worker, "_effort_shadow", None)
    if st is None:
        st = {"state": EffortState(level=level, floor=floor), "streak": {},
              "seen_verdict": None, "seen_verify": 0, "seen_fresh": 0}
        worker._effort_shadow = st
    return st


def build_signals(worker, ctx, cfg):
    """Signals from data already on the worker. Streaks are edge-tracked in `ctx`."""
    turn = int(getattr(worker, "turn", 0) or 0)
    budget = int(getattr(worker, "max_turns", 0) or 0)
    verdict = getattr(worker, "_last_refute_verdict", "") or ""
    rc = int(getattr(worker, "refute_count", 0) or 0)
    key = (verdict, rc)
    up = ctx.setdefault("upheld", 0)
    refuted = ctx.setdefault("refuted", 0)
    if verdict and key != ctx.get("seen_verdict"):
        ctx["seen_verdict"] = key
        if verdict == "REFUTED":
            ctx["refuted"] = refuted = refuted + 1
            ctx["upheld"] = up = 0
        elif verdict == "UPHELD":
            ctx["upheld"] = up = (up + 1) if rc <= 1 else 0
    outcome = str(getattr(worker, "outcome", "") or "").upper()
    status = str(getattr(worker, "status", "") or "").lower()
    fresh = int(getattr(worker, "fresh_replay_count", 0) or 0)
    troubled = (outcome in _TROUBLE_OUTCOMES or status in ("stuck", "content_refused")
                or fresh > ctx.get("seen_fresh", 0))
    ctx["seen_fresh"] = fresh
    ctx["stuck"] = (ctx.get("stuck", 0) + 1) if troubled else 0
    va = int(getattr(worker, "verify_attempts", 0) or 0)
    vfail = va > ctx.get("seen_verify", 0)
    ctx["seen_verify"] = va
    # no_progress is the worker's own counter and stays high while a stall lasts. Measured
    # from the value it had when the last switch consumed it, so one stall is one piece of
    # evidence and not a fresh escalation on every later turn.
    npv = int(getattr(worker, "no_progress", 0) or 0)
    ctx["np_base"] = base = min(ctx.get("np_base", 0), npv)
    return Signals(
        refuted_count=refuted, upheld_first_pass_streak=up,
        stuck_or_refused_streak=ctx["stuck"],
        no_progress_turns=npv - base,
        retries_used=int(getattr(worker, "transient", 0) or 0),
        verify_failed=vfail, confidence_low=0, turns_used=turn, turn_budget=budget,
        budget_pressure=bool(budget and turn >= cfg.budget_pressure_frac * budget))


def shadow_tick(worker, *, record=None, log=None, env=None):
    """Called once per decided turn. Records what the policy would do; changes nothing.

    NEVER RAISES, and does nothing at all when the mode is off: measurement must not be able
    to fail a run, and the default must be byte-for-byte the old behaviour.
    """
    try:
        if mode(env, log) == "off":
            return None
        cfg = PolicyConfig.from_env(env)
        knobs = {"refuter": getattr(worker, "refuter", None),
                 "max_refute": getattr(worker, "max_refute", None),
                 "max_research": getattr(worker, "max_research", None),
                 "review_lenses": getattr(worker, "review_lenses", None)}
        run_level = level_of_knobs(knobs)
        goal = getattr(worker, "goal", None)
        explicit = effort_mod.goal_effort(goal if isinstance(goal, dict) else {})
        floor = explicit if explicit in LADDER else "min"
        ctx = _shadow_state(worker, run_level if run_level in LADDER else "auto", floor)
        sig = build_signals(worker, ctx["streak"], cfg)
        state = ctx["state"]
        before = state.level
        d = evaluate(state, sig, cfg, turn=sig.turns_used)
        apply(state, d, sig.turns_used)
        if d.action != "stay":
            # A switch CONSUMES the evidence that caused it; otherwise a REFUTED verdict
            # seen once would justify an escalation on every later turn.
            st = ctx["streak"]
            st["refuted"] = st["upheld"] = st["stuck"] = 0
            st["np_base"] = int(getattr(worker, "no_progress", 0) or 0)
        (record or _default_record)(
            "effort_policy", run_id=getattr(worker, "run_id", "") or "",
            instance=str(getattr(worker, "name", "") or ""), turn=sig.turns_used,
            configured=True, config_source=state.source, config_value=before,
            eligible=True, triggered=(d.action != "stay"),
            not_triggered_reason=(d.reason if d.action == "stay" else ""),
            executed=False, changed_decision=False, before=before, after=d.target_level,
            extra={"event": "turn", "decision": d.action, "reason": d.reason,
                   "level": before, "target": d.target_level, "signals": sig.as_dict(),
                   "mode": "shadow"})
        return d
    except Exception:
        return None
