"""Tests for the solve-policy router. Run: python -m relay.test_solve_policy"""
from relay import solve_policy as P
from relay.selfimprove.archive import genome_id


def _report(weak_pass, weak_n, strong_pass, strong_n):
    """A synthetic calibration_report() with one weak and one strong well-sampled class."""
    def blk(p, n):
        return {"n": n, "resolved": int(round(p * n)), "pass_at_1": p, "ci_low": 0.0, "ci_high": 0.0}
    return {
        "by_class": {"sphinx-doc": blk(weak_pass, weak_n), "django": blk(strong_pass, strong_n)},
        "overall": blk((weak_pass + strong_pass) / 2, weak_n + strong_n),
        "n_records_read": weak_n + strong_n, "n_evalerr_excluded": 0,
    }


_BASE = {"knobs": {"SWE_STRONG_SELFTEST": "1"}, "cards": {}, "parent_id": None, "note": "incumbent"}


def test_weak_class_routes_to_best_of_n():
    rep = _report(0.50, 12, 0.90, 30)
    plan = P.plan_solve("sphinx-doc__sphinx-1", rep, n=4, base_genome=_BASE)
    assert plan["mode"] == "best-of-N"
    assert plan["n"] == 4 and len(plan["genomes"]) == 4
    assert plan["genomes"][0] == _BASE            # attempt 0 is the incumbent
    ids = [genome_id(g) for g in plan["genomes"]]
    assert len(set(ids)) == 4                     # all distinct
    print("ok test_weak_class_routes_to_best_of_n")


def test_strong_class_routes_to_single_shot():
    rep = _report(0.50, 12, 0.90, 30)
    plan = P.plan_solve("django__django-1", rep, n=4, base_genome=_BASE)
    assert plan["mode"] == "single-shot" and plan["n"] == 1
    assert plan["genomes"] == [_BASE]
    print("ok test_strong_class_routes_to_single_shot")


def test_data_poor_class_defaults_to_best_of_n():
    rep = _report(0.50, 12, 0.90, 30)
    # a class with no measured history -> recommend_effort returns best-of-N (caution)
    plan = P.plan_solve("brandnew__brandnew-1", rep, n=3, base_genome=_BASE)
    assert plan["mode"] == "best-of-N"
    print("ok test_data_poor_class_defaults_to_best_of_n")


def test_finalize_delegates_to_decide():
    preds = [
        {"instance_id": "x__x-1", "model_patch": "--- a\n+++ b\n@@ -1 +1 @@\n-o\n+fix",
         "selftest_passed": True, "refuter_refuted": 0, "refuter_total": 2},
        {"instance_id": "x__x-1", "model_patch": "", "selftest_passed": None},
    ]
    d = P.finalize(preds)
    assert d["winner"]["instance_id"] == "x__x-1" and d["winner_idx"] == 0
    assert d["abstain"] is False
    # empty -> abstain
    assert P.finalize([])["winner"] is None
    print("ok test_finalize_delegates_to_decide")


def test_plan_and_explain_and_fallback():
    rep = _report(0.50, 12, 0.90, 30)
    s = P.plan_and_explain("sphinx-doc__sphinx-1", rep, n=4)
    assert "best-of-4" in s and "sphinx-doc" in s
    # garbage report -> safe fallback single-shot, never raises
    plan = P.plan_solve("y__y-1", {"by_class": "garbage"}, base_genome=_BASE)
    assert plan["mode"] == "single-shot"
    print("ok test_plan_and_explain_and_fallback")


if __name__ == "__main__":
    test_weak_class_routes_to_best_of_n()
    test_strong_class_routes_to_single_shot()
    test_data_poor_class_defaults_to_best_of_n()
    test_finalize_delegates_to_decide()
    test_plan_and_explain_and_fallback()
    print("ALL SOLVE_POLICY TESTS PASSED")
