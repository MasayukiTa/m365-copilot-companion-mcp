"""A per-goal effort POLICY: when should one goal's effort change, on what evidence.

PHASE 2 OF 5. With MCP_EFFORT_POLICY=shadow (phase 1) it records, per turn, the decision it
WOULD have taken; with the default (off) it does nothing at all. `on` (phase 2) makes ONE
thing real: the INITIAL effort of fan-out children (one step below the parent, the merge turn
keeping the parent's level) and the sibling de-escalation rule. In-run switching (evaluate)
is still record-only in every mode. See docs/private/20260930_effort_policy_design.md.

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
    #: LATER siblings of a fan-out campaign start one step lower once this many finished
    #: siblings in a row were UPHELD on the first refuter pass (env ..._SIBLING_UPHELD_STREAK).
    sibling_upheld_streak: int = 2
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
_warned_conflict = set()

#: (path, mtime_ns, size) -> value read from settings.txt. One os.stat per mode() call, a
#: re-read only when the file changed, so a change made in the cockpit is seen by the NEXT
#: mode() call in a RUNNING process (the next worker, fan-out or turn evaluation) with no
#: restart. Work already decided keeps its decision: this is a per-call read, not a live push.
_settings_cache = {}


def _settings_value(env):
    """`effort_policy=` from settings.txt, lower-cased, or None (absent/invalid/unreadable).

    Path: with the real environment (env is None) the repository's one resolver
    (tools.settings_path.settings_file, the same file settings_effort reads). With an injected
    env dict, ONLY the file named by its MCP_EFFORT_POLICY_SETTINGS entry, so a test that
    injects `env` can never be steered by the developer's real .config. First match wins, as in
    settings_effort; a BOM is tolerated. Never raises.
    """
    try:
        if env is None:
            from tools.settings_path import settings_file
            path = settings_file()
        else:
            path = str(env.get("MCP_EFFORT_POLICY_SETTINGS", "") or "")
        if not path:
            return None
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = None
        with open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("effort_policy="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else None
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return None


def _say(log, msg):
    try:
        if log:
            log(msg)
        else:
            import sys
            sys.stderr.write(msg + "\n")
    except Exception:
        pass


def mode_info(env=None, log=None):
    """(mode, source, conflict). source is env | settings | default.

    RESOLUTION ORDER: a valid MCP_EFFORT_POLICY in the environment > `effort_policy=` in
    settings.txt (the cockpit's setting) > off. The environment wins by documented precedence,
    which is exactly how a hand-passed value silently beats the screen; so when the two are
    both valid and DIFFER, `conflict` is True and it is logged once per (env, settings) pair,
    for a UI or the telemetry to show what is really in effect. Never raises.
    """
    try:
        e = os.environ if env is None else env
        raw = str(e.get("MCP_EFFORT_POLICY", "") or "").strip().lower()
        cfg = _settings_value(env)
        if raw in MODES:
            conflict = cfg is not None and cfg != raw
            if conflict and (raw, cfg) not in _warned_conflict:
                _warned_conflict.add((raw, cfg))
                _say(log, "[effort_policy] env MCP_EFFORT_POLICY=%s overrides settings.txt "
                          "effort_policy=%s" % (raw, cfg))
            return raw, "env", conflict
        if cfg is not None:
            return cfg, "settings", False
    except Exception:
        pass
    return "off", "default", False


def mode(env=None, log=None):
    """Effective policy mode: off|shadow|on. Order: env MCP_EFFORT_POLICY > settings.txt
    `effort_policy` > off (see mode_info). Unknown values are ignored.

    `on` = initial assignment (fan-out children, merge, sibling rule) is ACTIVE; live
    switching (evaluate) is still shadow. Said once per process so nobody believes more is
    steering than is.
    """
    m = mode_info(env, log)[0]
    if m == "on" and not _warned_on[0]:
        _warned_on[0] = True
        if log:
            try:
                log("[effort_policy] effort policy=on: initial assignment active "
                    "(fan-out children, sibling de-escalation); live switching still shadow")
            except Exception:
                pass
    return m


def _src(env):
    try:
        return mode_info(env)[1]
    except Exception:
        return "default"


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
                  calibration_level=None, sibling_streak=0, sibling_threshold=2):
    """The level a goal STARTS at, and where that came from. Returns (level, source).

    Precedence: explicit goal effort > parent-derived > calibration > run level.
    A fan-out child sits ONE STEP BELOW its parent (floor min); the merge/verify turn of a
    parent keeps the parent's level, because it judges the children's combined work.
    A sibling streak (finished siblings UPHELD first pass in a row) >= the threshold starts a
    LATER sibling one step lower still (floor min); source "sibling". Not for merge turns.
    Unknown names at any layer are skipped, not trusted.
    """
    explicit = effort_mod.goal_effort(goal)
    if explicit in LADDER:
        return explicit, "goal"
    if parent_level in LADDER:
        if is_merge_turn:
            return parent_level, "parent"
        lowered = step(parent_level, -1)
        if sibling_threshold > 0 and sibling_streak >= sibling_threshold:
            lower = step(lowered, -1)
            if lower != lowered:
                return lower, "sibling"
        return lowered, "parent"
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
# campaign evidence (phase 2): siblings of one fan-out campaign share what they learned
# ---------------------------------------------------------------------------------------
class CampaignEvidence:
    """Consecutive first-pass UPHELD count among FINISHED siblings, per campaign id.

    Pure and bounded: at most `max_campaigns` campaigns are remembered (least recently
    touched forgotten first). `observe(cid, True)` extends the streak; `observe(cid, False)`
    (a REFUTED / STUCK / failed sibling) resets it. The key is the campaign id fanout.py
    already mints; nothing here invents one.
    """

    def __init__(self, max_campaigns=256):
        from collections import OrderedDict
        self.max_campaigns = max(1, int(max_campaigns))
        self._streak = OrderedDict()

    def observe(self, cid, first_pass_upheld):
        if not cid:
            return 0
        n = self._streak.pop(cid, 0)
        n = n + 1 if first_pass_upheld else 0
        self._streak[cid] = n
        while len(self._streak) > self.max_campaigns:
            self._streak.popitem(last=False)
        return n

    def streak(self, cid):
        return self._streak.get(cid, 0) if cid else 0

    def reset(self):
        self._streak.clear()


EVIDENCE = CampaignEvidence()


def _meta(goal):
    m = goal.get("metadata") if isinstance(goal, dict) else None
    return m if isinstance(m, dict) else {}


def _cid_of(goal):
    return str(goal.get("campaign_id") or "") if isinstance(goal, dict) else ""


def assign_children(kids, parent_level, *, run_id="", record=None, log=None, env=None):
    """Initial effort for fan-out children. off: untouched. shadow: record "would assign".
    on: set metadata effort (one step below `parent_level`) and record "assigned".

    Returns `kids`. NEVER RAISES; a child that cannot be processed is left as it was.
    """
    try:
        m = mode(env, log)
        if m == "off" or parent_level not in LADDER:
            return kids
        rec = record or _default_record
        for k in kids:
            try:
                level, source = initial_level(k, "auto", parent_level=parent_level)
                if source != "parent":
                    continue                      # an explicit goal effort wins; leave it
                if m == "on":
                    meta = k.get("metadata")
                    if not isinstance(meta, dict):
                        meta = k["metadata"] = {}
                    meta["effort"] = level
                    meta["effort_source"] = "parent"
                    meta["parent_effort"] = parent_level
                rec("effort_policy", run_id=run_id, instance=str(k.get("task_id") or ""),
                    turn=0, configured=True, config_source="parent", config_value=level,
                    eligible=True, triggered=(m == "on"), executed=(m == "on"),
                    changed_decision=(m == "on"), before=parent_level, after=level,
                    extra={"event": "child", "mode_source": _src(env), "mode": m, "parent_level": parent_level,
                           "campaign_id": _cid_of(k), "subtask_index": k.get("subtask_index"),
                           "verb": "assigned" if m == "on" else "would assign"})
            except Exception:
                continue
    except Exception:
        pass
    return kids


def merge_effort(item, parent_level, *, run_id="", record=None, log=None, env=None):
    """The merge/verify turn keeps the PARENT's level (on only). Returns `item`. Never raises."""
    try:
        if mode(env, log) != "on" or parent_level not in LADDER or not isinstance(item, dict):
            return item
        level, source = initial_level(item, "auto", parent_level=parent_level,
                                      is_merge_turn=True)
        if source != "parent":
            return item
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            meta = item["metadata"] = {}
        meta["effort"], meta["effort_source"], meta["parent_effort"] = level, "parent", parent_level
        (record or _default_record)(
            "effort_policy", run_id=run_id, instance=str(item.get("task_id") or ""), turn=0,
            configured=True, config_source="parent", config_value=level, eligible=True,
            triggered=True, executed=True, changed_decision=True, before=parent_level,
            after=level, extra={"event": "merge", "mode_source": _src(env), "mode": "on", "verb": "assigned",
                                "campaign_id": _cid_of(item)})
    except Exception:
        pass
    return item


def sibling_adjust(goal, *, run_id="", instance="", record=None, log=None, env=None,
                   evidence=None):
    """At worker creation: a LATER sibling of a campaign whose finished siblings were UPHELD
    first pass `sibling_upheld_streak` times running starts one step lower than its
    parent-derived level (floor min). on: returns a copy of `goal` with the lower effort.
    shadow: records "would lower", returns `goal` unchanged. Never raises.
    """
    try:
        m = mode(env, log)
        if m == "off" or not isinstance(goal, dict) or goal.get("role") != "subtask":
            return goal
        cid = _cid_of(goal)
        streak = (evidence or EVIDENCE).streak(cid)
        cfg = PolicyConfig.from_env(env)
        if cfg.sibling_upheld_streak <= 0 or streak < cfg.sibling_upheld_streak:
            return goal
        meta = _meta(goal)
        cur = meta.get("effort")
        if meta.get("effort_source") == "parent":
            new = step(cur, -1)
        elif m == "shadow":
            cur, new = "", ""                     # shadow children carry no derived level
        else:
            return goal                            # explicit effort wins
        if m == "on" and (new == cur or new not in LADDER):
            return goal                            # already at the floor
        (record or _default_record)(
            "effort_policy", run_id=run_id, instance=instance or str(goal.get("task_id") or ""),
            turn=0, configured=True, config_source="sibling", config_value=new or "lower",
            eligible=True, triggered=(m == "on"), executed=(m == "on"),
            changed_decision=(m == "on"), before=cur or "", after=new or "lower",
            extra={"event": "sibling", "mode_source": _src(env), "mode": m, "campaign_id": cid, "streak": streak,
                   "verb": "lowered" if m == "on" else "would lower"})
        if m != "on":
            return goal
        out = dict(goal)
        out["metadata"] = dict(meta, effort=new, effort_source="sibling")
        return out
    except Exception:
        return goal


def worker_level(worker):
    """The ladder level a worker is running at (from the knobs it was given), else None."""
    try:
        lvl = level_of_knobs({"refuter": getattr(worker, "refuter", None),
                              "max_refute": getattr(worker, "max_refute", None),
                              "max_research": getattr(worker, "max_research", None),
                              "review_lenses": getattr(worker, "review_lenses", None)})
        return lvl if lvl in LADDER else None
    except Exception:
        return None


_FINISHED = ("done", "stuck", "maxturns", "error", "cancelled", "content_refused")


def observe_child(worker, *, env=None, evidence=None):
    """Feed a finished subtask's outcome into the campaign evidence. Never raises.

    first pass UPHELD -> extend the streak; REFUTED at any point, or any non-DONE terminal
    (STUCK, refused, error...) -> reset; a DONE with no refuter verdict is no evidence.
    Once per worker.
    """
    try:
        if mode(env) == "off":
            return None
        envelope = getattr(worker, "task_envelope", None)
        if getattr(envelope, "role", "") != "subtask":
            return None
        if str(getattr(worker, "status", "") or "").lower() not in _FINISHED:
            return None
        if getattr(worker, "_effort_observed", False):
            return None
        worker._effort_observed = True
        outcome = str(getattr(worker, "outcome", "") or "").upper()
        verdict = str(getattr(worker, "_last_refute_verdict", "") or "").upper()
        rc = int(getattr(worker, "refute_count", 0) or 0)
        if outcome != "DONE" or verdict == "REFUTED" or rc > 1:
            good = False
        elif verdict == "UPHELD":
            good = True
        else:
            return None
        return (evidence or EVIDENCE).observe(getattr(envelope, "campaign_id", ""), good)
    except Exception:
        return None


# ---------------------------------------------------------------------------------------
# shadow hooks (the only functions relay_fleet calls)
# ---------------------------------------------------------------------------------------
_TROUBLE_OUTCOMES = ("STUCK", "INFRA_STUCK", "REFUSED", "CONTENT_REFUSED")


def level_of_knobs(knobs):
    """Name the ladder level whose knobs equal `knobs`, else "custom".

    The lenses compare as lists with None == [] (a worker stores its lenses as [] where the
    level table says None); the other knobs compare as they are.
    """
    def norm(k, v):
        return list(v or []) if k == "review_lenses" else v
    try:
        for name in LADDER:
            spec = effort_mod.LEVELS[name]
            if all(norm(k, spec[k]) == norm(k, knobs.get(k)) for k in effort_mod.KNOBS):
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
        if source == "goal" and _meta(goal).get("effort_source") in ("parent", "sibling"):
            source = _meta(goal)["effort_source"]      # derived by this policy, not asked for
        (record or _default_record)(
            "effort_policy", run_id=run_id, instance=instance, turn=0,
            configured=True, config_source=source, config_value=level,
            eligible=True, triggered=False, executed=False, changed_decision=False,
            before=run_level, after=level,
            extra={"event": "initial", "run_level": run_level, "mode_source": _src(env), "mode": mode(env)})
        return level, source
    except Exception:
        return None


def _shadow_state(worker, level, floor):
    st = getattr(worker, "_effort_shadow", None)
    if st is None:
        st = {"state": EffortState(level=level, floor=floor), "streak": {},
              "seen_verdict": None, "seen_verify": 0, "seen_fresh": 0}
        worker._effort_shadow = st
        worker.effort_state = st["state"]     # read by status_fields (the cockpit badge)
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


def status_fields(worker, env=None):
    """Additive per-worker fields for status.json; {} when the policy is off or unknowable.

    effort_level is the level the worker REALLY runs at (from its knobs); effort_source is why
    (EffortState.source once the shadow state exists, else what the goal's metadata says).
    effort_last_switch is the last virtual switch and is ALWAYS record_only: live switching
    changes nothing. Absent key = the cockpit shows no badge. Never raises.
    """
    try:
        if mode(env) == "off":
            return {}
        level = worker_level(worker)
        if level is None:
            return {}
        out = {"effort_level": level}
        # worker.goal is the goal TEXT; the dict (effort, metadata) is worker.goal_record.
        goal = getattr(worker, "goal_record", None)
        goal = goal if isinstance(goal, dict) else {}
        src = _meta(goal).get("effort_source")
        base = (src if src in ("parent", "sibling") else
                "goal" if effort_mod.goal_effort(goal) in LADDER else "run")
        state = getattr(worker, "effort_state", None)
        # An unswitched state still says "run"; the goal knows better. Only a (virtual)
        # switch changes the source the policy itself would report.
        out["effort_source"] = (state.source if state is not None and getattr(state, "source", "")
                                in ("policy", "escalation") else base)
        if state is not None and getattr(state, "history", None):
            t, a, b, why = state.history[-1]
            out["effort_last_switch"] = {"turn": int(t), "from": a, "to": b,
                                         "reason": str(why), "record_only": True}
        return out
    except Exception:
        return {}


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
                   "mode_source": _src(env), "mode": "shadow"})   # live switching is record-only in every mode
        return d
    except Exception:
        return None
