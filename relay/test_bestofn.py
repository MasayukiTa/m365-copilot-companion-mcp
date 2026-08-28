"""Unit tests for the best-of-N selector. Run: python -m relay.test_bestofn"""
from relay import bestofn as B


def _cand(idx, diff, selftest=None, refuted=0, total=0, size=None):
    return {
        "idx": idx,
        "diff": diff,
        "selftest_passed": selftest,
        "refuter_refuted": refuted,
        "refuter_total": total,
        "diff_size": size,
    }


# A small real-looking diff body, reused with different headers to test normalization.
_BODY = "-    return x\n+    return x + 1\n"


def _wrap(file_a, line):
    """Same +/- edit, different file header + hunk line numbers -> must normalize identically."""
    return (
        "diff --git a/%s b/%s\n" % (file_a, file_a)
        + "index 1111111..2222222 100644\n"
        + "--- a/%s\n" % file_a
        + "+++ b/%s\n" % file_a
        + "@@ -%d,3 +%d,3 @@ def f():\n" % (line, line)
        + _BODY
    )


def test_selftest_dominance():
    # selftest True must beat selftest False even when False has bigger consensus.
    passed = _cand(0, "+    fix_a\n-    bug_a\n", selftest=True)
    # three identical failing candidates -> consensus 3 for the failing change
    fail1 = _cand(1, "+    fix_b\n-    bug_b\n", selftest=False)
    fail2 = _cand(2, "+    fix_b\n-    bug_b\n", selftest=False)
    fail3 = _cand(3, "+    fix_b\n-    bug_b\n", selftest=False)
    res = B.select_best([passed, fail1, fail2, fail3])
    assert res["winner"]["idx"] == 0, res["ranking"]
    print("ok test_selftest_dominance")


def test_consensus_breaks_ties():
    # Equal selftest/refuter; the one in the largest normalized-diff cluster wins.
    a1 = _cand(0, "+    shared\n-    old\n")          # cluster of 3
    a2 = _cand(1, "+    shared\n-    old\n")
    a3 = _cand(2, "+    shared\n-    old\n")
    b1 = _cand(3, "+    lone\n-    old\n")            # singleton
    res = B.select_best([b1, a1, a2, a3])
    assert res["winner"]["idx"] in (0, 1, 2), res["ranking"]
    assert res["ranking"][0]["consensus_size"] == 3
    # the singleton must rank last
    assert res["ranking"][-1]["idx"] == 3
    print("ok test_consensus_breaks_ties")


def test_empty_diff_floored():
    # An empty / whitespace-only diff never wins when a non-empty candidate exists, even if the
    # empty one would otherwise look attractive (e.g. selftest unknown but big "consensus").
    empty1 = _cand(0, "   \n\n  ", selftest=True)        # whitespace-only, even "passed"
    empty2 = _cand(1, "", selftest=True)
    real = _cand(2, "+    real change\n-    old\n", selftest=None)
    res = B.select_best([empty1, empty2, real])
    assert res["winner"]["idx"] == 2, res["ranking"]
    # both empties must rank below the real candidate
    order = [r["idx"] for r in res["ranking"]]
    assert order.index(2) < order.index(0) and order.index(2) < order.index(1)
    print("ok test_empty_diff_floored")


def test_refuter_survival_ranks():
    # All else equal, the more-refuted candidate ranks below the less-refuted one.
    less = _cand(0, "+    same\n-    old\n", refuted=0, total=5)   # survived 5/5
    more = _cand(1, "+    other\n-    old\n", refuted=4, total=5)  # survived 1/5
    res = B.select_best([more, less])
    assert res["winner"]["idx"] == 0, res["ranking"]
    order = [r["idx"] for r in res["ranking"]]
    assert order.index(0) < order.index(1)
    print("ok test_refuter_survival_ranks")


def test_minimality_tiebreak():
    # Equal on everything measurable, the smaller diff wins.
    small = _cand(0, "+    a\n-    b\n", size=2)
    big = _cand(1, "+    c\n-    d\n", size=50)
    res = B.select_best([big, small])
    assert res["winner"]["idx"] == 0, res["ranking"]
    print("ok test_minimality_tiebreak")


def test_normalize_groups_same_change():
    # Two patches: same +/- content, different file path header AND different hunk line numbers.
    p1 = _wrap("pkg/mod.py", 10)
    p2 = _wrap("pkg/mod.py", 99)            # only the @@ position differs
    assert B._normalize_diff(p1) == B._normalize_diff(p2)
    c1 = _cand(0, p1)
    c2 = _cand(1, p2)
    cons = B.consensus([c1, c2])
    assert cons[0] == 2 and cons[1] == 2, cons        # clustered together

    # A genuinely different change must NOT group with them.
    other = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-    return y\n+    return y - 1\n"
    c3 = _cand(2, other)
    cons3 = B.consensus([c1, c2, c3])
    assert cons3[0] == 2 and cons3[1] == 2 and cons3[2] == 1, cons3
    print("ok test_normalize_groups_same_change")


def test_empties_are_singletons():
    # Two empty diffs must NOT cluster together -- agreeing on "no change" is not convergence.
    e1 = _cand(0, "")
    e2 = _cand(1, "   ")
    cons = B.consensus([e1, e2])
    assert cons[0] == 1 and cons[1] == 1, cons
    print("ok test_empties_are_singletons")


def test_empty_selection():
    res = B.select_best([])
    assert res["winner"] is None
    assert res["ranking"] == []
    assert res["rationale"] == "no candidates"
    print("ok test_empty_selection")


def test_ranking_shape():
    # Ranking entries carry exactly the documented keys, best->worst, and are deterministic.
    cands = [
        _cand(2, "+    x\n-    y\n", selftest=True, total=3, refuted=0),
        _cand(0, "+    x\n-    y\n", selftest=True, total=3, refuted=0),
        _cand(1, "+    z\n-    y\n", selftest=False),
    ]
    res = B.select_best(cands)
    for e in res["ranking"]:
        assert set(e.keys()) == {"idx", "score", "consensus_size", "selftest_passed"}
    # idx 0 and 2 share the same change + signals; tie-break by lower idx -> 0 wins over 2.
    assert res["winner"]["idx"] == 0, res["ranking"]
    # deterministic: re-running gives identical ranking order
    res2 = B.select_best(cands)
    assert [e["idx"] for e in res["ranking"]] == [e["idx"] for e in res2["ranking"]]
    print("ok test_ranking_shape")


if __name__ == "__main__":
    test_selftest_dominance()
    test_consensus_breaks_ties()
    test_empty_diff_floored()
    test_refuter_survival_ranks()
    test_minimality_tiebreak()
    test_normalize_groups_same_change()
    test_empties_are_singletons()
    test_empty_selection()
    test_ranking_shape()
    print("ALL BESTOFN TESTS PASSED")


# ---- what this selector cannot judge -----------------------------------------------------

def test_prose_candidates_are_refused_rather_than_ranked_by_length():
    """THE MEASURED INVERSION. Every signal here is read from a unified diff, and
    `_normalize_diff` keeps only +/- lines -- so prose normalises to empty, every candidate
    becomes its own singleton, and the self-consistency signal that best-of-N uniquely buys
    disappears. Minimality is all that survives, so the SHORTEST answer wins.

    Measured on three text answers where two were identical and one disagreed: the selector
    returned the odd one out because it was shortest. Two candidates agreeing was invisible to
    it, and it returned a confident rationale while doing it."""
    import pytest

    from relay.bestofn import select_best
    texts = ["土日の予定は6件です。", "土日の予定は3件でした。", "土日の予定は6件です。"]
    cands = [{"idx": i, "diff": t, "diff_size": len(t)} for i, t in enumerate(texts)]
    with pytest.raises(ValueError) as exc:
        select_best(cands)
    assert "diff content" in str(exc.value)


def test_a_single_candidate_is_still_selected_even_if_empty():
    """N=1 has no consensus to lose, and an empty patch is a legitimate outcome to report --
    refusing it would break the ordinary path that selects one capture."""
    from relay.bestofn import select_best
    r = select_best([{"idx": 0, "diff": "", "diff_size": 0}])
    assert r["winner"] is not None


def test_real_diffs_are_unaffected():
    """The guard must not close the door on the case the selector exists for."""
    from relay.bestofn import select_best
    d = ["--- a\n+++ b\n+one", "--- a\n+++ b\n+two"]
    r = select_best([{"idx": i, "diff": x, "diff_size": len(x)} for i, x in enumerate(d)])
    assert r["winner"]["idx"] in (0, 1)


def test_a_mix_of_prose_and_diff_is_not_refused():
    """The refusal is for a set the selector can say nothing about. If ANY candidate carries
    diff content, consensus and minimality are meaningful again for that one."""
    from relay.bestofn import select_best
    cands = [{"idx": 0, "diff": "ただの文章です", "diff_size": 7},
             {"idx": 1, "diff": "--- a\n+++ b\n+real", "diff_size": 17}]
    assert select_best(cands)["winner"]["idx"] == 1
