"""Hermetic, offline tests for the general-use quality validate driver (quality_loop.py).

Everything here is deterministic and stdlib-only. The core (run_arm / judge_arm / gate_arms /
validate / check_degradation) is exercised with INJECTED fake runner/judge/usage functions, so no
fleet, no bridge, no network, no clock-dependent behaviour. The relay env hook (implementation A) is
verified separately under its own module's import check, not here.

Run: python -m relay.selfimprove.test_quality_loop  -> exit 0 + "ALL QUALITY LOOP TESTS PASSED".
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.selfimprove import quality_loop as QL
from relay.selfimprove import quality as Q


# --------------------------------------------------------------------------------------------------
# Fixtures: dirty (persona leaks) vs clean (plain factual) outputs.
# --------------------------------------------------------------------------------------------------
# These exact strings are what the real heuristic also flags/clears, so the judge_fn=None fallback
# path and the explicit-fake-judge path agree.
DIRTY = "まずは基礎を完璧に固めろ。今の理解レベルだと初心者の9割は詰む。"
CLEAN = "マージ(merge)を基本に使う。push済み・共有済みの履歴は rebase しない。"


def _suite(n):
    """A deterministic probe suite of size n."""
    return [{"id": "p%d" % i, "prompt": "probe %d" % i} for i in range(n)]


def make_runner(baseline_dirty_ids, proposed_dirty_ids):
    """Build a fake runner. A probe gets DIRTY text iff its id is in the arm's dirty set, else CLEAN.

    The arm is identified by discipline_override is None (baseline) vs not-None (proposed).
    """
    def runner(probe_suite, discipline_override):
        dirty = baseline_dirty_ids if discipline_override is None else proposed_dirty_ids
        out = {}
        for p in probe_suite:
            pid = p["id"]
            out[pid] = DIRTY if pid in dirty else CLEAN
        return out
    return runner


def fake_judge(text):
    """Deterministic judge: DIRTY marker -> leak(True), else clean(False)."""
    return text == DIRTY


# --------------------------------------------------------------------------------------------------
# run_arm / judge_arm
# --------------------------------------------------------------------------------------------------

def test_run_and_judge_arm():
    suite = _suite(4)
    runner = make_runner(baseline_dirty_ids={"p0", "p1", "p2"}, proposed_dirty_ids={"p0"})

    base_out = QL.run_arm("baseline", None, suite, runner)
    prop_out = QL.run_arm("proposed", "OVERRIDE-TEXT", suite, runner)
    assert set(base_out.keys()) == {"p0", "p1", "p2", "p3"}, base_out
    assert base_out["p0"] == DIRTY and base_out["p3"] == CLEAN

    base_leaks = QL.judge_arm(base_out, fake_judge)
    prop_leaks = QL.judge_arm(prop_out, fake_judge)
    assert base_leaks == {"p0": True, "p1": True, "p2": True, "p3": False}, base_leaks
    assert prop_leaks == {"p0": True, "p1": False, "p2": False, "p3": False}, prop_leaks

    # judge_fn=None must fall back to the real heuristic and produce the SAME verdicts here.
    base_leaks_heur = QL.judge_arm(base_out, None)
    assert base_leaks_heur == base_leaks, base_leaks_heur


def test_run_arm_filters_unknown_and_coerces():
    suite = _suite(2)

    def noisy_runner(probe_suite, discipline_override):
        return {"p0": CLEAN, "p1": None, "p_unknown": DIRTY}  # None + an unsolicited probe id

    out = QL.run_arm("x", None, suite, noisy_runner)
    assert set(out.keys()) == {"p0", "p1"}, out          # unknown id dropped
    assert out["p1"] == "", out                          # None coerced to ""


def test_judge_arm_flaky_judge_falls_back():
    # a judge that raises on every call must degrade to the heuristic, not abort the arm
    def boom(_text):
        raise RuntimeError("judge down")

    out = {"p0": DIRTY, "p1": CLEAN}
    res = QL.judge_arm(out, boom)
    assert res == {"p0": True, "p1": False}, res


# --------------------------------------------------------------------------------------------------
# gate_arms: keep / non-positive / underpowered
# --------------------------------------------------------------------------------------------------

def test_gate_keep_on_clear_improvement():
    # Large enough N with a strong, significant clean-rate lift -> keep.
    ids = ["p%d" % i for i in range(20)]
    # baseline: first 12 leak; proposed: none leak. 12 helped pairs, 0 hurt -> significant + positive.
    baseline = dict((i, i in set(ids[:12])) for i in ids)
    proposed = dict((i, False) for i in ids)
    gate = QL.gate_arms(baseline, proposed, ids, min_n=10)
    assert gate["keep"] is True, gate
    assert gate["verdict"] == "keep", gate
    assert gate["baseline_clean"] == 8 and gate["proposed_clean"] == 20, gate
    assert "proposed clean 20/20" in gate["summary"], gate["summary"]


def test_gate_non_positive_when_no_improvement():
    ids = ["p%d" % i for i in range(20)]
    # proposed is WORSE: baseline all clean, proposed leaks 6 -> net negative -> non-positive.
    baseline = dict((i, False) for i in ids)
    proposed = dict((i, i in set(ids[:6])) for i in ids)
    gate = QL.gate_arms(baseline, proposed, ids, min_n=10)
    assert gate["keep"] is False, gate
    assert gate["verdict"] == "non-positive", gate


def test_gate_underpowered_when_below_powered_floor():
    # When the operator demands more power than the suite has (min_n > n), the gate returns
    # verdict="underpowered" -> recommend "enlarge", never auto-commit. (With the DEFAULT
    # min_n=len(ids) the floor is exactly met, so a tiny-but-directionally-good suite lands on
    # "suggestive" instead -- both map to enlarge; see test_validate_recommendation_enlarge_*.)
    ids = ["p0", "p1", "p2"]
    baseline = {"p0": True, "p1": True, "p2": True}     # all leak
    proposed = {"p0": False, "p1": False, "p2": False}  # all clean
    gate = QL.gate_arms(baseline, proposed, ids, min_n=10)  # demand 10, only have 3
    assert gate["keep"] is False, gate
    assert gate["verdict"] == "underpowered", gate
    assert gate["probe_n"] == 3, gate


def test_gate_small_good_suite_is_suggestive_not_keep():
    # Default min_n=len(ids): the powered floor is met, but 3 paired flips is not significant
    # (McNemar p=0.25) -> "suggestive", still keep=False. The recommendation layer maps it to enlarge.
    ids = ["p0", "p1", "p2"]
    baseline = {"p0": True, "p1": True, "p2": True}
    proposed = {"p0": False, "p1": False, "p2": False}
    gate = QL.gate_arms(baseline, proposed, ids)        # min_n defaults to 3
    assert gate["keep"] is False, gate
    assert gate["verdict"] == "suggestive", gate
    assert QL._recommendation(gate) == "enlarge", gate


def test_gate_missing_probe_counts_as_not_clean():
    # a probe absent from an arm's verdicts is read conservatively as "leaked" (not a clean win).
    ids = ["p0", "p1"]
    baseline = {"p0": True}            # p1 missing -> treated as leak
    proposed = {"p0": False}           # p1 missing -> treated as leak
    gate = QL.gate_arms(baseline, proposed, ids, min_n=2)
    assert gate["baseline_clean"] == 0 and gate["proposed_clean"] == 1, gate


# --------------------------------------------------------------------------------------------------
# validate: report structure + recommendation
# --------------------------------------------------------------------------------------------------

def test_validate_report_keep():
    suite = _suite(20)
    base_dirty = set("p%d" % i for i in range(12))
    runner = make_runner(baseline_dirty_ids=base_dirty, proposed_dirty_ids=set())
    rep = QL.validate("PROPOSED DISCIPLINE TEXT", runner_fn=runner, judge_fn=fake_judge,
                      probe_suite=suite, min_n=10)

    assert set(rep.keys()) == {"probe_n", "baseline_clean", "proposed_clean",
                               "gate", "recommendation", "proposed_excerpt"}, rep
    assert rep["probe_n"] == 20, rep
    assert rep["baseline_clean"] == 8 and rep["proposed_clean"] == 20, rep
    assert rep["recommendation"] == "keep", rep
    assert rep["proposed_excerpt"] == "PROPOSED DISCIPLINE TEXT", rep


def test_validate_recommendation_enlarge_on_small_suite():
    suite = _suite(4)
    runner = make_runner(baseline_dirty_ids={"p0", "p1", "p2", "p3"}, proposed_dirty_ids=set())
    rep = QL.validate("X", runner_fn=runner, judge_fn=fake_judge, probe_suite=suite)
    assert rep["recommendation"] == "enlarge", rep
    # default min_n == probe count: a tiny good suite is "suggestive" (not significant) -> enlarge.
    assert rep["gate"]["verdict"] in ("underpowered", "suggestive"), rep


def test_validate_recommendation_revert_on_regression():
    suite = _suite(20)
    # proposed makes things worse -> non-positive -> revert (n=20 >= min_n=10, so really measured)
    runner = make_runner(baseline_dirty_ids=set(), proposed_dirty_ids=set("p%d" % i for i in range(8)))
    rep = QL.validate("X", runner_fn=runner, judge_fn=fake_judge, probe_suite=suite, min_n=10)
    assert rep["recommendation"] == "revert", rep
    assert rep["gate"]["verdict"] == "non-positive", rep


def test_validate_default_suite_runs():
    # the shipped PROBE_SUITE must flow through validate with an injected runner (offline)
    def all_clean_runner(probe_suite, discipline_override):
        return dict((p["id"], CLEAN) for p in probe_suite)

    rep = QL.validate("X", runner_fn=all_clean_runner, judge_fn=fake_judge)
    assert rep["probe_n"] == len(QL.PROBE_SUITE), rep
    assert rep["baseline_clean"] == rep["probe_n"], rep


# --------------------------------------------------------------------------------------------------
# check_degradation: threshold judgement with injected usage_fn
# --------------------------------------------------------------------------------------------------

def test_check_degradation_trips_above_threshold():
    res = QL.check_degradation(0.15, usage_fn=lambda: {"persona_leak_rate": 0.40})
    assert res["tripped"] is True, res
    assert res["leak_rate"] == 0.40 and res["threshold"] == 0.15, res


def test_check_degradation_holds_below_threshold():
    res = QL.check_degradation(0.15, usage_fn=lambda: {"persona_leak_rate": 0.10})
    assert res["tripped"] is False, res


def test_check_degradation_none_rate_never_trips():
    res = QL.check_degradation(0.15, usage_fn=lambda: {"persona_leak_rate": None})
    assert res["tripped"] is False and res["leak_rate"] is None, res


def test_check_degradation_broken_usage_is_defensive():
    def boom():
        raise RuntimeError("usage module broken")

    res = QL.check_degradation(0.15, usage_fn=boom)
    assert res["tripped"] is False and res["leak_rate"] is None, res


# --------------------------------------------------------------------------------------------------
# probe suite sanity
# --------------------------------------------------------------------------------------------------

def test_probe_suite_is_well_formed():
    ids = QL.probe_ids()
    assert len(ids) == len(set(ids)), "probe ids must be unique"
    assert len(ids) >= 5, "spec wants merge/async/review/learn-first/rest-graphql + a few"
    for p in QL.PROBE_SUITE:
        assert p.get("id") and p.get("prompt"), p


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("ran %d quality-loop tests" % len(tests))


if __name__ == "__main__":
    _run_all()
    print("ALL QUALITY LOOP TESTS PASSED")
