"""Unit tests for the best-of-N orchestration glue. Run: python -m relay.test_bestofn_run

Hermetic + deterministic: no fleet, no network, no subprocess; the only file reads are against a
tempdir created inside the test. Composes relay.bestofn (selector) + relay.confidence (calibration)
through relay.bestofn_run.
"""
import json
import os
import tempfile

from relay import bestofn_run as R


# a tiny self-test-passing diff and a couple of distinct real diffs (normalize to different clusters)
_DIFF_A = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-old_a\n+new_a\n"
_DIFF_B = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-old_b\n+new_b\n"
_DIFF_C = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-old_c\n+new_c\n"


def test_candidates_from_preds():
    preds = [
        {"instance_id": "t1", "model_patch": _DIFF_A, "selftest_passed": True,
         "refuter_refuted": 1, "refuter_total": 3},
        {"instance_id": "t1", "diff": _DIFF_B},                       # patch under "diff" key
        {"instance_id": "t1"},                                        # missing patch -> ""
        "not-a-dict",                                                 # skipped entirely
        {"instance_id": "t1", "model_patch": _DIFF_C, "selftest_passed": False},
    ]
    cands = R.candidates_from_preds(preds)
    # the non-dict entry is skipped -> 4 candidates survive.
    assert len(cands) == 4
    # idx preserves input order (the non-dict at position 3 is skipped, not renumbered).
    assert [c["idx"] for c in cands] == [0, 1, 2, 4]
    # field mapping + defaults
    assert cands[0]["diff"] == _DIFF_A and cands[0]["selftest_passed"] is True
    assert cands[0]["refuter_refuted"] == 1 and cands[0]["refuter_total"] == 3
    assert cands[1]["diff"] == _DIFF_B                                # picked up from "diff"
    assert cands[1]["selftest_passed"] is None                        # default unknown
    assert cands[1]["refuter_refuted"] == 0 and cands[1]["refuter_total"] == 0
    assert cands[2]["diff"] == ""                                     # missing patch -> empty
    assert cands[3]["diff"] == _DIFF_C and cands[3]["selftest_passed"] is False
    print("ok test_candidates_from_preds")


def test_decide_clear_winner():
    # one attempt passes its self-test, others do not -> it must win, high-ish, do not abstain.
    preds = [
        {"instance_id": "win__1", "model_patch": _DIFF_A, "selftest_passed": True,
         "refuter_refuted": 0, "refuter_total": 2},
        {"instance_id": "win__1", "model_patch": _DIFF_B, "selftest_passed": False},
        {"instance_id": "win__1", "model_patch": _DIFF_C},            # unknown selftest
    ]
    d = R.decide(preds)
    assert d["n"] == 3
    assert d["winner_idx"] == 0
    assert d["winner"] is not None
    assert d["winner"]["instance_id"] == "win__1"                     # carried through from record
    assert d["winner"]["diff"] == _DIFF_A
    assert d["abstain"] is False
    assert d["level"] in ("high", "medium")                          # passing self-test -> not low
    assert "self-test passed" in d["explain"]
    print("ok test_decide_clear_winner")


def test_decide_diverged_abstains():
    # all different real diffs, NONE passed its self-test, no refuters -> no consensus, near-tie:
    # the fleet could not agree -> abstain True, escalate True.
    preds = [
        {"instance_id": "div__1", "model_patch": _DIFF_A},
        {"instance_id": "div__1", "model_patch": _DIFF_B},
        {"instance_id": "div__1", "model_patch": _DIFF_C},
    ]
    d = R.decide(preds)
    assert d["n"] == 3
    assert d["winner_idx"] is not None                               # it still names a winner...
    assert d["abstain"] is True                                      # ...but refuses to silently ship
    assert d["escalate"] is True
    assert d["level"] == "low"
    print("ok test_decide_diverged_abstains")


def test_decide_empty():
    d = R.decide([])
    assert d["winner"] is None
    assert d["winner_idx"] is None
    assert d["abstain"] is True
    assert d["escalate"] is True
    assert d["n"] == 0
    assert d["ranking"] == []
    assert d["explain"] == "no candidates"
    # an all-non-dict input also yields zero candidates -> same humble result.
    d2 = R.decide(["x", 5, None])
    assert d2["winner"] is None and d2["abstain"] is True and d2["n"] == 0
    print("ok test_decide_empty")


def test_winner_instance_id_carried():
    # winner is the self-test passer at idx 1; its instance_id must be the one returned, even when
    # records disagree on instance_id (use the winner's).
    preds = [
        {"instance_id": "other", "model_patch": _DIFF_B, "selftest_passed": False},
        {"instance_id": "the_real_one", "model_patch": _DIFF_A, "selftest_passed": True},
    ]
    d = R.decide(preds)
    assert d["winner_idx"] == 1
    assert d["winner"]["instance_id"] == "the_real_one"
    print("ok test_winner_instance_id_carried")


def test_load_candidate_dir():
    with tempfile.TemporaryDirectory() as dpath:
        # write N=3 one-element-list captures, intentionally out of name order to prove sorting.
        captures = {
            "attempt_02.json": [{"instance_id": "z__1", "model_patch": _DIFF_C}],
            "attempt_00.json": [{"instance_id": "z__1", "model_patch": _DIFF_A}],
            "attempt_01.json": [{"instance_id": "z__1", "model_patch": _DIFF_B}],
        }
        for name, payload in captures.items():
            with open(os.path.join(dpath, name), "w", encoding="utf-8") as f:
                json.dump(payload, f)
        # a non-json file and a malformed json file must both be skipped (never raise).
        with open(os.path.join(dpath, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("ignore me")
        with open(os.path.join(dpath, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json")

        recs = R.load_candidate_dir(dpath)
        assert len(recs) == 3                                        # 3 good captures, 2 skipped
        # sorted by filename -> attempt_00, _01, _02 -> diffs A, B, C in that order.
        assert [r["model_patch"] for r in recs] == [_DIFF_A, _DIFF_B, _DIFF_C]

        # the loaded records feed straight into decide().
        d = R.decide(recs)
        assert d["n"] == 3 and d["winner"]["instance_id"] == "z__1"

    # missing directory -> [] (no raise).
    assert R.load_candidate_dir(os.path.join(dpath, "does_not_exist")) == []
    print("ok test_load_candidate_dir")


if __name__ == "__main__":
    test_candidates_from_preds()
    test_decide_clear_winner()
    test_decide_diverged_abstains()
    test_decide_empty()
    test_winner_instance_id_carried()
    test_load_candidate_dir()
    print("ALL BESTOFN_RUN TESTS PASSED")
