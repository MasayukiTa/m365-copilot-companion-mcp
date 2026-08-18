"""Unit tests for the self-improvement guardrails. Run: python -m relay.selfimprove.test_guards"""
import os
import subprocess
import sys
import tempfile
import time
import uuid

from relay.selfimprove import guards as G


def test_burned_registry():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "burned.jsonl")
        r = G.BurnedRegistry(path)
        assert len(r) == 0
        added = r.add(["a__a-1", "b__b-2"], reason="diagnosis")
        assert added == 2
        assert r.add(["b__b-2"], reason="ab") == 0           # already burned -> skipped
        assert r.is_burned("a__a-1") and not r.is_burned("c__c-3")
        assert r.filter_fresh(["a__a-1", "c__c-3", "c__c-3"]) == ["c__c-3"]  # burned + dup removed
        r2 = G.BurnedRegistry(path)                           # reload from disk
        assert r2.is_burned("a__a-1") and len(r2) == 2
    print("ok test_burned_registry")


def test_overfit_lint():
    assert G.overfit_lint("trace the symptom through the edited function before DONE") == []
    assert G.is_domain_general("check exact output, not just 'does not crash'")
    bad = G.overfit_lint("in django__django-12345 patch django/forms/widgets.py and test_merge")
    assert any(v.startswith("instance_id:") for v in bad)
    assert any(v.startswith("source_path:") for v in bad)
    assert any(v.startswith("test_name:") for v in bad)
    assert any(v.startswith("repo_name:django") for v in bad)
    assert not G.is_domain_general("fix sympy printer output")
    print("ok test_overfit_lint")


def test_mcnemar_and_gate():
    # exact McNemar sanity
    assert G.mcnemar_exact_p(0, 0) == 1.0
    assert abs(G.mcnemar_exact_p(7, 2) - 0.180) < 0.01      # the 2026-06-21 discordant pair

    ids = ["i%03d" % i for i in range(80)]
    on = set(ids[:62]) | set(ids[62:69])                    # 69 resolved (62 both + 7 only-on)
    off = set(ids[:62]) | set(ids[69:71])                   # 64 resolved (62 both + 2 only-off)
    g = G.significance_gate(on, off, ids, min_n=100)
    # 2026-06-21 FIXTURE: +6.2pp, p=0.18, N=80 -> must NOT keep; verdict underpowered/suggestive
    assert g["keep"] is False
    assert g["verdict"] in ("underpowered", "suggestive")
    assert g["b"] == 7 and g["c"] == 2 and g["n"] == 80
    assert abs(g["net_pp"] - 6.25) < 0.1

    # scale the same effect 5x (N=400, b=35, c=10) -> should clear the gate
    big = ["j%04d" % i for i in range(400)]
    onb = set(big[:310]) | set(big[310:345])                # both 310 + 35 only-on
    offb = set(big[:310]) | set(big[345:355])               # both 310 + 10 only-off
    gb = G.significance_gate(onb, offb, big, min_n=100)
    assert gb["keep"] is True and gb["verdict"] == "keep" and gb["p"] < 0.05

    # a regression (off better) must never keep
    gr = G.significance_gate(off, on, ids, min_n=10)
    assert gr["keep"] is False and gr["verdict"] == "non-positive"
    print("ok test_mcnemar_and_gate")


def test_classify_and_partition():
    assert G.classify_outcome("RESOLVED") == "resolved"
    assert G.classify_outcome("EVALERR") == "infra"
    assert G.classify_outcome("not", "swebench returned EVALERR for this id") == "infra"
    assert G.classify_outcome("not", "consent card: 書き込む内容を教えてください") == "infra"
    assert G.classify_outcome("not", "", patch="") == "infra"          # empty capture = infra
    assert G.classify_outcome("not", "FAIL_TO_PASS still failing", patch="diff --git ...") == "real"
    recs = [
        {"instance_id": "r1", "verdict": "RESOLVED"},
        {"instance_id": "r2", "verdict": "not", "log_tail": "real failing assertion", "patch": "x"},
        {"instance_id": "r3", "verdict": "EVALERR"},
        {"instance_id": "r4", "verdict": "not", "patch": ""},
    ]
    part = G.partition_outcomes(recs)
    assert part["resolved"] == ["r1"] and part["real_miss"] == ["r2"] and sorted(part["infra"]) == ["r3", "r4"]
    print("ok test_classify_and_partition")


def test_done_after_last_start():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("solve start\nsolve done/paused\n")      # previous run finished
            f.write("solve start\nchunk 1/4\n")              # current run still going
        # the stale done line precedes the latest start -> current run NOT done
        assert G.done_after_last_start(p, "solve start", "solve done/paused") is False
        with open(p, "a", encoding="utf-8") as f:
            f.write("solve done/paused\n")
        assert G.done_after_last_start(p, "solve start", "solve done/paused") is True
    print("ok test_done_after_last_start")


def test_proc_alive():
    # Use a dedicated marker instead of assuming pytest's parent command line
    # contains this test module's filename during a full-suite run.
    marker = "proc_alive_test_" + uuid.uuid4().hex
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker]
    )
    try:
        for _ in range(40):
            if G.proc_alive(marker) >= 1:
                break
            time.sleep(0.05)
        assert G.proc_alive(marker) >= 1
        assert G.proc_alive("a_substring_that_should_match_nothing_zzz") == 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    print("ok test_proc_alive")


if __name__ == "__main__":
    test_burned_registry()
    test_overfit_lint()
    test_mcnemar_and_gate()
    test_classify_and_partition()
    test_done_after_last_start()
    test_proc_alive()
    print("ALL GUARD TESTS PASSED")


# ---------------------------------------------------------------------------
# Zero discordant pairs is zero information, and must not be reported as a verdict on the
# candidate. Found by running the closed loop: four episodes, all four passing on BOTH arms,
# came back as REJECT with "net +0.0 pp is not an improvement". The prediction written before
# the run said INCONCLUSIVE, and the prediction was right.
# ---------------------------------------------------------------------------

def test_no_pair_disagreed_is_underpowered_not_a_verdict():
    """McNemar reads the pairs that DISAGREED; concordant pairs cancel and say nothing.

    With b=c=0 the test has no power at all, so "the change did nothing" and "this sample
    could not have detected anything" are the same observation -- and only one of them is a
    statement about the candidate. `n < min_n` did not catch it because that counts PAIRS,
    and four pairs clears any small threshold; the quantity that must be large enough is the
    discordant count.
    """
    ids = ["a", "b", "c", "d"]
    got = G.significance_gate(ids, ids, ids, min_n=1)
    assert got["b"] == 0 and got["c"] == 0
    assert got["verdict"] == "underpowered", "情報ゼロを候補への判定として報告している"
    assert got["keep"] is False
    assert "no pair disagreed" in got["reason"]


def test_a_real_disagreement_is_still_judged_normally():
    """情報ゼロだけを取り除く -- 実際に差が出ているケースの扱いは変えない。"""
    ids = ["a", "b", "c", "d"]
    hurt = G.significance_gate(["a", "b"], ["a", "b", "c"], ids, min_n=1)
    assert hurt["verdict"] == "non-positive"
    helped = G.significance_gate(["a", "b", "c"], ["a", "b"], ids, min_n=1)
    assert helped["verdict"] == "suggestive"


def test_an_empty_slice_is_still_underpowered_for_the_original_reason():
    got = G.significance_gate([], [], [], min_n=1)
    assert got["verdict"] == "underpowered"
    assert "min_n" in got["reason"]
