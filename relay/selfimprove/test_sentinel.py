"""Unit tests for the cross-dataset sentinel. Run: python -m relay.selfimprove.test_sentinel"""
import os
import tempfile

from relay.selfimprove import sentinel as S


def test_sentinel_members_and_baseline():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sentinel.json")
        s = S.Sentinel(path)
        assert s.members() == [] and s.baseline() == set()
        s.set_members(["a__a-1", "b__b-2", "c__c-3", "a__a-1"])     # dup dropped
        s.set_baseline(["a__a-1", "b__b-2", "zzz__zzz-9"])          # non-member clamped out
        s.note = "fixed cross-dataset canary; reused every iter; not a headline score"
        s.save()
        s2 = S.Sentinel(path)                                       # reload from disk
        assert s2.members() == ["a__a-1", "b__b-2", "c__c-3"]
        assert s2.baseline() == {"a__a-1", "b__b-2"}
        assert "headline" in s2.note
    print("ok test_sentinel_members_and_baseline")


def test_check_no_regression():
    with tempfile.TemporaryDirectory() as d:
        s = S.Sentinel(os.path.join(d, "sentinel.json"))
        s.set_members(["a__a-1", "b__b-2", "c__c-3"])
        s.set_baseline(["a__a-1", "b__b-2"])
        # (a) candidate resolves all baseline (and a new one) -> not regressed
        r = s.check(["a__a-1", "b__b-2", "c__c-3"])
        assert r["regressed"] is False
        assert r["lost"] == [] and r["gained"] == ["c__c-3"]
        assert r["n_members"] == 3 and r["n_baseline"] == 2 and r["n_candidate_on_sentinel"] == 3
    print("ok test_check_no_regression")


def test_check_regression():
    with tempfile.TemporaryDirectory() as d:
        s = S.Sentinel(os.path.join(d, "sentinel.json"))
        s.set_members(["a__a-1", "b__b-2", "c__c-3"])
        s.set_baseline(["a__a-1", "b__b-2"])
        # (b) candidate drops one baseline id -> regressed True, that id in "lost"
        r = s.check(["a__a-1", "c__c-3"])
        assert r["regressed"] is True
        assert r["lost"] == ["b__b-2"]
        assert r["gained"] == ["c__c-3"]
    print("ok test_check_regression")


def test_sentinel_verdict():
    no_reg = {"regressed": False, "lost": [], "gained": []}
    reg = {"regressed": True, "lost": ["b__b-2"], "gained": []}
    # (c) gate-keep True turns to keep False when the sentinel regressed
    v = S.sentinel_verdict(True, reg)
    assert v["keep"] is False and "b__b-2" in v["reason"] and "REGRESSED" in v["reason"]
    # gate-keep True with no regression stays True
    v2 = S.sentinel_verdict(True, no_reg)
    assert v2["keep"] is True
    # (d) gate-keep False stays False regardless of the sentinel
    assert S.sentinel_verdict(False, no_reg)["keep"] is False
    assert S.sentinel_verdict(False, reg)["keep"] is False
    print("ok test_sentinel_verdict")


if __name__ == "__main__":
    test_sentinel_members_and_baseline()
    test_check_no_regression()
    test_check_regression()
    test_sentinel_verdict()
    print("ALL SENTINEL TESTS PASSED")
