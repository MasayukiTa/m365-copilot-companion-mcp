"""Unit tests for per-task confidence + abstention. Run: python -m relay.test_confidence

Hermetic: pure functions over candidate-signal dicts (relay/bestofn.py shape); no fleet, no network,
no subprocess, no disk. Mirrors relay/selfimprove/test_guards.py style.
"""
from relay import bestofn
from relay import confidence as C


def _cand(idx, diff, selftest=None, refuted=0, total=0):
    return {
        "idx": idx,
        "diff": diff,
        "selftest_passed": selftest,
        "refuter_refuted": refuted,
        "refuter_total": total,
        "diff_size": None,
    }


# A real (non-empty) diff body. Same body across candidates => they cluster (consensus).
_DIFF_A = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new_A\n"


def _diff(tag):
    return "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+%s\n" % tag


def test_high_confidence():
    # 4 candidates, ALL identical passing diff, refuters survived -> full consensus, hard pass.
    cands = [_cand(i, _DIFF_A, selftest=True, refuted=0, total=2) for i in range(4)]
    sel = bestofn.select_best(cands)
    conf = C.task_confidence(cands)
    assert conf["winner_idx"] is not None
    assert conf["level"] == "high", conf
    assert conf["abstain"] is False
    assert C.should_escalate(conf) is False
    assert conf["confidence"] >= C.CONF_WEIGHTS["high_thresh"]
    assert abs(conf["consensus_fraction"] - 1.0) < 1e-9
    # explain never raises and names the winner.
    s = C.explain(conf, sel)
    assert "picked candidate" in s and "self-test passed" in s
    print("ok test_high_confidence")


def test_low_confidence_abstain():
    # 4 candidates, ALL distinct diffs (no convergence), none passing self-test, refuters all refuted.
    cands = [
        _cand(0, _diff("a"), selftest=False, refuted=2, total=2),
        _cand(1, _diff("b"), selftest=False, refuted=2, total=2),
        _cand(2, _diff("c"), selftest=False, refuted=2, total=2),
        _cand(3, _diff("d"), selftest=False, refuted=2, total=2),
    ]
    conf = C.task_confidence(cands)
    assert conf["level"] == "low", conf
    assert conf["abstain"] is True, conf
    assert C.should_escalate(conf) is True
    # singleton winner among 4 -> consensus fraction 1/4.
    assert abs(conf["consensus_fraction"] - 0.25) < 1e-9
    sel = bestofn.select_best(cands)
    s = C.explain(conf, sel)
    assert "ABSTAIN" in s
    print("ok test_low_confidence_abstain")


def test_medium_band():
    # Neither high nor low: unknown self-test (neutral), full consensus, neutral refuters (none).
    # f_selftest=0.5, f_consensus=1.0, f_refuter=0.5, f_margin=0 (all identical) ->
    # (0.40*0.5 + 0.20*1.0 + 0.25*0.5 + 0.15*0)/1.0 = 0.525 -> medium.
    cands = [_cand(i, _DIFF_A, selftest=None, refuted=0, total=0) for i in range(3)]
    conf = C.task_confidence(cands)
    assert conf["level"] == "medium", conf
    assert conf["confidence"] < C.CONF_WEIGHTS["high_thresh"]
    assert conf["confidence"] >= C.CONF_WEIGHTS["low_thresh"]
    # medium + no hard pass: still not silently shipped only if low; medium does NOT abstain.
    assert conf["abstain"] is False
    print("ok test_medium_band")


def test_margin_matters():
    # Same factors EXCEPT lead size. A clear single winner (one passing, rest failing) must be more
    # confident than a near-tie (one marginally ahead). All else (self-test of winner) held equal by
    # giving the winner selftest=True in both; only the runner-up's strength differs.

    # Clear lead: winner passes, the other three FAIL their self-test -> large score gap.
    clear = [
        _cand(0, _diff("w"), selftest=True, refuted=0, total=2),
        _cand(1, _diff("x"), selftest=False, refuted=2, total=2),
        _cand(2, _diff("y"), selftest=False, refuted=2, total=2),
        _cand(3, _diff("z"), selftest=False, refuted=2, total=2),
    ]
    # Near-tie: winner passes, runner-up ALSO passes with equally strong refuters -> tiny score gap.
    tie = [
        _cand(0, _diff("w"), selftest=True, refuted=0, total=2),
        _cand(1, _diff("x"), selftest=True, refuted=0, total=2),
        _cand(2, _diff("y"), selftest=True, refuted=0, total=2),
        _cand(3, _diff("z"), selftest=True, refuted=0, total=2),
    ]
    c_clear = C.task_confidence(clear)
    c_tie = C.task_confidence(tie)
    # The clear-lead winner is strictly more confident, all else equal.
    assert c_clear["confidence"] > c_tie["confidence"], (c_clear, c_tie)
    print("ok test_margin_matters")


def test_empty_candidates():
    conf = C.task_confidence([])
    assert conf["confidence"] == 0.0
    assert conf["abstain"] is True
    assert conf["level"] == "low"
    assert conf["winner_idx"] is None
    assert conf["n_candidates"] == 0
    assert C.should_escalate(conf) is True
    # explain must not raise on the empty result (no select_result winner).
    s = C.explain(conf, {"winner": None, "ranking": [], "rationale": "no candidates"})
    assert isinstance(s, str) and "abstain" in s.lower()
    print("ok test_empty_candidates")


def test_empty_diff_winner_low():
    # The only "candidate" proposes no edit -> empty-diff floor -> minimal confidence, abstain.
    cands = [_cand(0, "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n", selftest=None)]
    conf = C.task_confidence(cands)
    assert conf["confidence"] == 0.0
    assert conf["level"] == "low"
    assert conf["abstain"] is True
    print("ok test_empty_diff_winner_low")


def test_explain_never_raises():
    # Garbage select_result must not crash explain.
    conf = {"winner_idx": 1, "n_candidates": 3, "confidence": 0.5, "level": "medium", "abstain": False}
    assert isinstance(C.explain(conf, None), str)
    assert isinstance(C.explain(conf, {"ranking": "not-a-list"}), str)
    print("ok test_explain_never_raises")


if __name__ == "__main__":
    test_high_confidence()
    test_low_confidence_abstain()
    test_medium_band()
    test_margin_matters()
    test_empty_candidates()
    test_empty_diff_winner_low()
    test_explain_never_raises()
    print("ALL CONFIDENCE TESTS PASSED")
