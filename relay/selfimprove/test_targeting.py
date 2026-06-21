"""Unit tests for the improvement targeter. Run: python -m relay.selfimprove.test_targeting

Hermetic: synthetic calibration report dicts + a synthetic grade_results.jsonl in a temp dir. No
network, no live ledger, deterministic.
"""
import json
import os
import tempfile

from relay.selfimprove import targeting as T


def _block(n, resolved):
    """Mimic calibration's by_class block shape (pass_at_1 + a cheap CI stand-in)."""
    p = (resolved / n) if n else None
    return {"n": n, "resolved": resolved, "pass_at_1": p,
            "ci_low": 0.0, "ci_high": 100.0}


def _report(by_class):
    return {"by_class": by_class, "overall": {"n": 0},
            "n_records_read": 0, "n_evalerr_excluded": 0}


def test_next_target_picks_weak_well_sampled():
    # weak+well-sampled (40%, n=20), strong (95%, n=20), data-poor (10%, n=2 -> below min_n).
    rep = _report({
        "weakrepo": _block(20, 8),     # 40% pass@1, lots of evidence -> the target
        "strongrepo": _block(20, 19),  # 95% -> no headroom under max_pass=0.9
        "poorrepo": _block(2, 0),      # 0% but n<min_n -> data-poor, skipped
    })
    t = T.next_target(rep, min_n=5, max_pass=0.9)
    assert t is not None
    assert t["task_class"] == "weakrepo"
    assert abs(t["pass_at_1"] - 0.4) < 1e-9 and t["n"] == 20
    assert abs(t["headroom"] - (0.9 - 0.4)) < 1e-9
    print("ok test_next_target_picks_weak_well_sampled")


def test_next_target_respects_min_n_max_pass_exclude_and_tiebreak():
    # min_n filter: raise min_n so the only weak class is excluded -> None.
    rep_poor = _report({"weakrepo": _block(4, 1)})       # n=4
    assert T.next_target(rep_poor, min_n=5, max_pass=0.9) is None

    # max_pass filter: everything strong -> None.
    rep_strong = _report({"a": _block(20, 19), "b": _block(20, 20)})
    assert T.next_target(rep_strong, min_n=5, max_pass=0.9) is None

    # exclude: weakest is excluded -> next weakest qualifies.
    rep = _report({
        "weakest": _block(20, 4),   # 20%
        "weaker": _block(20, 10),   # 50%
    })
    assert T.next_target(rep, exclude={"weakest"})["task_class"] == "weaker"

    # tie-break: equal pass@1 -> larger n wins; equal n -> class name.
    rep_tie = _report({
        "zsmall": _block(10, 5),    # 50%, n=10
        "abig": _block(40, 20),     # 50%, n=40  -> more evidence wins
    })
    assert T.next_target(rep_tie)["task_class"] == "abig"
    rep_name = _report({"bbb": _block(20, 10), "aaa": _block(20, 10)})  # all equal -> name
    assert T.next_target(rep_name)["task_class"] == "aaa"
    print("ok test_next_target_respects_min_n_max_pass_exclude_and_tiebreak")


def test_next_target_defensive():
    assert T.next_target(None) is None
    assert T.next_target({}) is None
    assert T.next_target({"by_class": "garbage"}) is None
    assert T.next_target({"by_class": {"x": "notadict"}}) is None
    assert T.next_target({"by_class": {"x": {"n": 9, "pass_at_1": None}}}) is None
    print("ok test_next_target_defensive")


def test_assemble_misses_excludes_resolved_and_evalerr_and_dedupes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "grade_results.jsonl")
        rows = [
            # weakrepo: a real miss, a resolved, an EVALERR (infra), and a re-graded instance.
            {"instance_id": "weakrepo__weakrepo-1", "verdict": "not", "ts": 1},
            {"instance_id": "weakrepo__weakrepo-2", "verdict": "RESOLVED", "ts": 1},
            {"instance_id": "weakrepo__weakrepo-3", "verdict": "EVALERR", "ts": 1},
            # instance-4 graded twice: latest (ts=2) is a miss -> counts; earlier RESOLVED ignored.
            {"instance_id": "weakrepo__weakrepo-4", "verdict": "RESOLVED", "ts": 1},
            {"instance_id": "weakrepo__weakrepo-4", "verdict": "not", "ts": 2},
            # instance-5 graded twice: latest (ts=2) is RESOLVED -> NOT a miss.
            {"instance_id": "weakrepo__weakrepo-5", "verdict": "not", "ts": 1},
            {"instance_id": "weakrepo__weakrepo-5", "verdict": "RESOLVED", "ts": 2},
            # a different class miss -> must not leak into weakrepo.
            {"instance_id": "otherrepo__otherrepo-9", "verdict": "not", "ts": 1},
        ]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        misses = T.assemble_misses("weakrepo", path)
        # real misses for weakrepo: instance-1 and instance-4 (latest verdict). Sorted, deterministic.
        assert misses == ["weakrepo__weakrepo-1", "weakrepo__weakrepo-4"], misses
        # other class isolated.
        assert T.assemble_misses("otherrepo", path) == ["otherrepo__otherrepo-9"]
        # unknown class -> empty.
        assert T.assemble_misses("nosuch", path) == []
    print("ok test_assemble_misses_excludes_resolved_and_evalerr_and_dedupes")


def test_assemble_misses_defensive():
    assert T.assemble_misses("weakrepo", os.path.join(tempfile.gettempdir(), "no_such_file_zzz.jsonl")) == []
    print("ok test_assemble_misses_defensive")


def test_improvement_plan_composes_and_no_target_note():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "grade_results.jsonl")
        rows = [
            {"instance_id": "weakrepo__weakrepo-1", "verdict": "not", "ts": 1},
            {"instance_id": "weakrepo__weakrepo-4", "verdict": "not", "ts": 1},
        ]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        # explicit report so the target is deterministic; misses read from the synthetic ledger.
        rep = _report({"weakrepo": _block(20, 8), "strongrepo": _block(20, 19)})
        plan = T.improvement_plan(rep, grade_results_path=path)
        assert plan["target"]["task_class"] == "weakrepo"
        assert plan["misses"] == ["weakrepo__weakrepo-1", "weakrepo__weakrepo-4"]
        assert "DOMAIN-GENERAL" in plan["note"] and "overfit_lint" in plan["note"]

        # everything strong-or-data-poor -> no target, with the guidance note.
        rep_none = _report({"strong": _block(20, 20), "poor": _block(2, 0)})
        plan_none = T.improvement_plan(rep_none, grade_results_path=path)
        assert plan_none["target"] is None and plan_none["misses"] == []
        assert "no weak class with enough data" in plan_none["note"]
    print("ok test_improvement_plan_composes_and_no_target_note")


def test_improvement_plan_defensive_missing_report():
    # report=None forces a calibration_report() read; point it at a missing ledger -> no target.
    missing = os.path.join(tempfile.gettempdir(), "no_such_ledger_zzz.jsonl")
    plan = T.improvement_plan(None, grade_results_path=missing)
    assert plan["target"] is None and plan["misses"] == []
    assert "no weak class with enough data" in plan["note"]
    print("ok test_improvement_plan_defensive_missing_report")


if __name__ == "__main__":
    test_next_target_picks_weak_well_sampled()
    test_next_target_respects_min_n_max_pass_exclude_and_tiebreak()
    test_next_target_defensive()
    test_assemble_misses_excludes_resolved_and_evalerr_and_dedupes()
    test_assemble_misses_defensive()
    test_improvement_plan_composes_and_no_target_note()
    test_improvement_plan_defensive_missing_report()
    print("ALL TARGETING TESTS PASSED")
