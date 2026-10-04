"""Phase 2 of the per-goal effort policy: fan-out children get an initial effort, the merge keeps
the parent's, and later siblings start lower once earlier siblings were UPHELD first pass.

All of it is behind MCP_EFFORT_POLICY=on. off and shadow must leave every child goal
byte-identical to what fanout.py produced before this phase.
"""
import copy

import pytest

from relay import effort as effort_mod
from relay import effort_policy as ep
from relay import fanout as fo
from relay import mechanism_telemetry as mt
from relay import relay_fleet as rf
from relay.test_fanout_worker import SPLIT_REPLY

ON = {"MCP_EFFORT_POLICY": "on"}
SHADOW = {"MCP_EFFORT_POLICY": "shadow"}
STEPS = ["read january", "read february", "read march"]
PARENT = "collect the quarter's business mail"
#: a goal the splittability judge accepts (same text relay/test_fanout_worker.py uses)
SPLITTABLE = "1〜3月のメールを一覧化する"


class Rec:
    def __init__(self):
        self.rows = []

    def __call__(self, mech, **kw):
        self.rows.append((mech, kw))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MCP_EFFORT_POLICY", raising=False)
    monkeypatch.delenv("MCP_EFFORT_POLICY_SIBLING_UPHELD_STREAK", raising=False)
    ep.EVIDENCE.reset()
    yield
    ep.EVIDENCE.reset()


def _mode(monkeypatch, value, rec=None):
    if value is None:
        monkeypatch.delenv("MCP_EFFORT_POLICY", raising=False)
    else:
        monkeypatch.setenv("MCP_EFFORT_POLICY", value)
    rec = rec if rec is not None else Rec()
    monkeypatch.setattr(ep, "_default_record", rec)
    return rec


# ---- the ladder round-trips ------------------------------------------------------------------

@pytest.mark.parametrize("name", ep.LADDER)
def test_every_level_round_trips_through_its_knobs(name):
    knobs = effort_mod.resolve({"effort": name}, {"refuter": None, "max_refute": None,
                                                  "max_research": None, "review_lenses": None})
    assert ep.level_of_knobs(knobs) == name


def test_a_hand_made_knob_set_is_custom_not_a_level():
    assert ep.level_of_knobs({"refuter": True, "max_refute": 9, "max_research": 9,
                              "review_lenses": None}) == "custom"


# ---- A: children and merge -------------------------------------------------------------------

def _kids(**kw):
    return fo.child_goals(PARENT, STEPS, parent_task_id="p1", **kw)


@pytest.mark.parametrize("value", [None, "off", "shadow", "garbage"])
def test_off_and_shadow_leave_child_goals_byte_identical(monkeypatch, value):
    baseline = _kids()
    _mode(monkeypatch, value)
    for lvl in ep.LADDER:
        got = _kids(parent_level=lvl, run_id="r")
        assert got == baseline and copy.deepcopy(got) == baseline
        assert all("metadata" not in k for k in got)


@pytest.mark.parametrize("value", [None, "off", "shadow"])
def test_off_and_shadow_leave_the_merge_goal_identical(monkeypatch, value):
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "x"}]
    baseline = fo.aggregation_goal(PARENT, recs, campaign_id="c1")
    _mode(monkeypatch, value)
    assert fo.aggregation_goal(PARENT, recs, campaign_id="c1", parent_level="ultra") == baseline


def test_off_writes_no_row_and_shadow_records_would_assign(monkeypatch):
    rec = _mode(monkeypatch, "off")
    _kids(parent_level="auto")
    assert rec.rows == []
    rec = _mode(monkeypatch, "shadow")
    _kids(parent_level="auto")
    assert len(rec.rows) == 3
    for mech, kw in rec.rows:
        assert mech == "effort_policy" and kw["extra"]["verb"] == "would assign"
        assert kw["executed"] is False and kw["changed_decision"] is False
        assert (kw["before"], kw["after"]) == ("auto", "max")


@pytest.mark.parametrize("parent,child", [("ultra", "auto"), ("auto", "max"),
                                          ("max", "min"), ("min", "min")])
def test_on_assigns_one_step_below_the_parent_with_a_floor(monkeypatch, parent, child):
    rec = _mode(monkeypatch, "on")
    kids = _kids(parent_level=parent, run_id="r9")
    for k in kids:
        assert k["metadata"]["effort"] == child
        assert k["metadata"]["effort_source"] == "parent"
        assert k["metadata"]["parent_effort"] == parent
        assert effort_mod.goal_effort(k) == child
    assert [kw["extra"]["verb"] for _, kw in rec.rows] == ["assigned"] * 3
    assert all(kw["run_id"] == "r9" and kw["config_source"] == "parent" for _, kw in rec.rows)


def test_the_assigned_effort_is_what_resolve_gives_the_worker(monkeypatch):
    _mode(monkeypatch, "on")
    k = _kids(parent_level="ultra")[0]
    knobs = effort_mod.resolve(k, {"refuter": None, "max_refute": None, "max_research": None,
                                   "review_lenses": None})
    assert ep.level_of_knobs(knobs) == "auto"


def test_on_without_a_known_parent_level_changes_nothing(monkeypatch):
    baseline = _kids()
    _mode(monkeypatch, "on")
    assert _kids(parent_level=None) == baseline
    assert _kids(parent_level="custom") == baseline


def test_the_merge_keeps_the_parents_level_not_one_below(monkeypatch):
    rec = _mode(monkeypatch, "on")
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "x"}]
    m = fo.aggregation_goal(PARENT, recs, campaign_id="c1", parent_level="ultra")
    assert m["metadata"]["effort"] == "ultra" and effort_mod.goal_effort(m) == "ultra"
    assert m["depth"] == fo.MAX_DEPTH and m["role"] == "aggregator"
    assert rec.rows[-1][1]["extra"]["event"] == "merge"


def test_an_explicit_goal_effort_beats_the_parent_derived_one(monkeypatch):
    _mode(monkeypatch, "on")
    assert ep.initial_level({"effort": "ultra"}, "auto", parent_level="max") == ("ultra", "goal")
    kids = [{"text": "t", "task_id": "c-1", "campaign_id": "c", "metadata": {"effort": "min"}}]
    ep.assign_children(kids, "ultra")
    assert kids[0]["metadata"] == {"effort": "min"}


def test_a_failing_record_cannot_break_child_creation(monkeypatch):
    _mode(monkeypatch, "on", rec=lambda *a, **k: 1 / 0)
    kids = _kids(parent_level="auto")
    assert [k["metadata"]["effort"] for k in kids] == ["max"] * 3
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "x"}]
    assert fo.aggregation_goal(PARENT, recs, campaign_id="c", parent_level="auto")


def test_a_real_parent_worker_hands_its_level_to_its_children(monkeypatch):
    _mode(monkeypatch, "on")
    got = []
    w = rf.RelayWorker({"text": SPLITTABLE, "depth": 0}, "w0", fanout=True,
                       refuter=True, max_refute=3, max_research=3, review_lenses=None,
                       spawn_fn=lambda goal, kids, parent_checks=None, parent_partial="":
                       got.append(kids))
    w._decide(SPLIT_REPLY)
    assert w.outcome == "FANOUT" and len(got[0]) == 3
    assert {k["metadata"]["effort"] for k in got[0]} == {"max"}


def test_a_worker_with_run_default_knobs_that_are_no_level_splits_unchanged(monkeypatch):
    _mode(monkeypatch, "on")
    got = []
    w = rf.RelayWorker({"text": SPLITTABLE, "depth": 0}, "w0", fanout=True,
                       spawn_fn=lambda goal, kids, parent_checks=None, parent_partial="":
                       got.append(kids))
    w._decide(SPLIT_REPLY)
    assert all("metadata" not in k for k in got[0])


# ---- B: mode ---------------------------------------------------------------------------------

def test_on_is_a_real_mode_now_and_says_what_it_does_once():
    ep._warned_on[0] = False
    msgs = []
    assert ep.mode(ON, msgs.append) == "on" and ep.mode(ON, msgs.append) == "on"
    assert len(msgs) == 1 and "initial assignment active" in msgs[0]
    assert "live switching still shadow" in msgs[0]


def test_live_switching_stays_shadow_under_on(monkeypatch):
    rec = _mode(monkeypatch, "on")

    class W:
        refuter, max_refute, max_research, review_lenses = True, 3, 3, None
        goal, turn, max_turns, status, outcome = "g", 1, 10, "ready", ""
        _last_refute_verdict, refute_count = "REFUTED", 1
        run_id, name = "r", "w0"

    d = ep.shadow_tick(W())
    assert d is not None and d.action == "up"
    _, kw = rec.rows[-1]
    assert kw["executed"] is False and kw["changed_decision"] is False
    assert kw["extra"]["mode"] == "shadow"


# ---- C: sibling de-escalation ----------------------------------------------------------------

def _sibling(cid="c1", level="auto"):
    return {"text": "t", "campaign_id": cid, "task_id": cid + "-3", "role": "subtask",
            "metadata": {"effort": level, "effort_source": "parent", "parent_effort": "ultra"}}


class FW:
    """Just enough of a finished worker for observe_child."""

    def __init__(self, cid="c1", outcome="DONE", verdict="UPHELD", rc=1, role="subtask",
                 status="done"):
        self.task_envelope = type("E", (), {"role": role, "campaign_id": cid})()
        self.outcome, self.status = outcome, status
        self._last_refute_verdict, self.refute_count = verdict, rc


def test_the_streak_counts_first_pass_upheld_and_any_bad_sibling_resets_it():
    ev = ep.CampaignEvidence()
    assert ev.observe("c", True) == 1 and ev.observe("c", True) == 2
    assert ev.observe("c", False) == 0 and ev.streak("c") == 0
    assert ev.observe("c", True) == 1


def test_evidence_is_per_campaign():
    ev = ep.CampaignEvidence()
    ev.observe("a", True), ev.observe("a", True), ev.observe("b", False)
    assert ev.streak("a") == 2 and ev.streak("b") == 0 and ev.streak("zzz") == 0
    assert ev.observe("", True) == 0 and ev.streak("") == 0


def test_evidence_memory_is_bounded_and_forgets_the_least_recently_touched():
    ev = ep.CampaignEvidence(max_campaigns=3)
    for c in "abcd":
        ev.observe(c, True)
    assert ev.streak("a") == 0 and ev.streak("d") == 1 and len(ev._streak) == 3
    ev.observe("b", True)
    ev.observe("e", True)
    assert ev.streak("b") == 2 and ev.streak("c") == 0


def test_observe_child_classifies_first_pass_upheld_refuted_and_stuck(monkeypatch):
    _mode(monkeypatch, "on")
    ep.observe_child(FW())
    assert ep.observe_child(FW()) == 2
    assert ep.observe_child(FW(verdict="REFUTED", rc=1)) == 0
    ep.observe_child(FW())
    assert ep.observe_child(FW(rc=2)) == 0            # upheld only after a refutation
    ep.observe_child(FW())
    assert ep.observe_child(FW(outcome="STUCK", status="stuck", verdict="")) == 0
    ep.observe_child(FW())
    assert ep.observe_child(FW(verdict="")) is None   # no refuter verdict: no evidence
    assert ep.EVIDENCE.streak("c1") == 1
    assert ep.observe_child(FW(role="producer")) is None
    assert ep.observe_child(FW(status="running")) is None


def test_a_worker_is_observed_once(monkeypatch):
    _mode(monkeypatch, "on")
    w = FW()
    assert ep.observe_child(w) == 1 and ep.observe_child(w) is None
    assert ep.EVIDENCE.streak("c1") == 1


def test_observe_child_does_nothing_when_off(monkeypatch):
    _mode(monkeypatch, None)
    assert ep.observe_child(FW()) is None and ep.EVIDENCE.streak("c1") == 0


def test_later_siblings_start_one_step_lower_after_the_streak_and_it_resets(monkeypatch):
    rec = _mode(monkeypatch, "on")
    kid = _sibling()
    assert ep.sibling_adjust(kid) is kid                      # no evidence yet
    ep.observe_child(FW())
    assert ep.sibling_adjust(kid) is kid                      # streak 1 < 2
    ep.observe_child(FW())
    low = ep.sibling_adjust(kid)
    assert low is not kid and low["metadata"]["effort"] == "max"
    assert low["metadata"]["effort_source"] == "sibling"
    assert kid["metadata"]["effort"] == "auto", "the original goal must not be mutated"
    assert rec.rows[-1][1]["extra"]["verb"] == "lowered"
    assert rec.rows[-1][1]["extra"]["streak"] == 2
    ep.observe_child(FW(verdict="REFUTED"))
    assert ep.sibling_adjust(kid) is kid                      # streak reset


def test_the_sibling_rule_floors_at_min(monkeypatch):
    _mode(monkeypatch, "on")
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = _sibling(level="min")
    assert ep.sibling_adjust(kid) is kid


def test_the_sibling_rule_never_touches_an_explicit_effort(monkeypatch):
    _mode(monkeypatch, "on")
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = {"text": "t", "campaign_id": "c1", "role": "subtask", "effort": "ultra"}
    assert ep.sibling_adjust(kid) is kid
    kid2 = {"text": "t", "campaign_id": "c1", "role": "subtask", "metadata": {"effort": "auto"}}
    assert ep.sibling_adjust(kid2) is kid2


def test_the_sibling_rule_only_applies_to_its_own_campaign(monkeypatch):
    _mode(monkeypatch, "on")
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    other = _sibling(cid="c2")
    assert ep.sibling_adjust(other) is other
    assert ep.sibling_adjust(_sibling(cid="c1"))["metadata"]["effort"] == "max"


def test_the_streak_threshold_is_configurable(monkeypatch):
    _mode(monkeypatch, "on")
    monkeypatch.setenv("MCP_EFFORT_POLICY_SIBLING_UPHELD_STREAK", "3")
    assert ep.PolicyConfig.from_env().sibling_upheld_streak == 3
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = _sibling()
    assert ep.sibling_adjust(kid) is kid
    ep.EVIDENCE.observe("c1", True)
    assert ep.sibling_adjust(kid)["metadata"]["effort"] == "max"
    assert ep.PolicyConfig().sibling_upheld_streak == 2


def test_in_shadow_the_sibling_rule_only_records_would_lower(monkeypatch):
    rec = _mode(monkeypatch, "shadow")
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = {"text": "t", "campaign_id": "c1", "task_id": "c1-3", "role": "subtask"}
    before = copy.deepcopy(kid)
    assert ep.sibling_adjust(kid) is kid and kid == before
    _, kw = rec.rows[-1]
    assert kw["extra"]["verb"] == "would lower" and kw["executed"] is False


def test_off_ignores_the_sibling_rule_entirely(monkeypatch):
    rec = _mode(monkeypatch, None)
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = _sibling()
    assert ep.sibling_adjust(kid) is kid and rec.rows == []


def test_sibling_telemetry_failure_returns_the_goal_unchanged(monkeypatch):
    _mode(monkeypatch, "on", rec=lambda *a, **k: 1 / 0)
    ep.EVIDENCE.observe("c1", True), ep.EVIDENCE.observe("c1", True)
    kid = _sibling()
    assert ep.sibling_adjust(kid) is kid


def test_initial_level_reports_source_sibling_and_never_lowers_a_merge():
    assert ep.initial_level({}, "auto", parent_level="ultra") == ("auto", "parent")
    assert ep.initial_level({}, "auto", parent_level="ultra", sibling_streak=2) == ("max", "sibling")
    assert ep.initial_level({}, "auto", parent_level="auto", sibling_streak=5) == ("min", "sibling")
    assert ep.initial_level({}, "auto", parent_level="max", sibling_streak=5) == ("min", "parent")
    assert ep.initial_level({}, "auto", parent_level="min", sibling_streak=5) == ("min", "parent")
    assert ep.initial_level({}, "auto", parent_level="ultra", is_merge_turn=True,
                            sibling_streak=9) == ("ultra", "parent")
    assert ep.initial_level({"effort": "ultra"}, "auto", parent_level="max",
                            sibling_streak=9) == ("ultra", "goal")


def test_the_fleet_runs_sibling_lowering_end_to_end(monkeypatch):
    """A real parent splits; two children finish first-pass UPHELD through the worker's own
    _decide hook; the third sibling is created one step lower."""
    _mode(monkeypatch, "on")
    got = []
    parent = rf.RelayWorker({"text": SPLITTABLE, "depth": 0}, "w0", fanout=True,
                            refuter=True, max_refute=3, max_research=3, review_lenses=None,
                            spawn_fn=lambda goal, kids, parent_checks=None, parent_partial="":
                            got.append(kids))
    parent._decide(SPLIT_REPLY)
    kids = got[0]
    assert {k["metadata"]["effort"] for k in kids} == {"max"}
    for k in kids[:2]:
        w = rf.RelayWorker(k, "wk", max_turns=5)
        w.status, w.outcome = "done", "DONE"
        w._last_refute_verdict, w.refute_count = "UPHELD", 1
        ep.observe_child(w)
    third = ep.sibling_adjust(kids[2])
    assert third["metadata"]["effort"] == "min"
    assert kids[2]["metadata"]["effort"] == "max"
