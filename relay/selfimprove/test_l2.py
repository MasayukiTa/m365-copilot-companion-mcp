"""Unit tests for the L2 iteration driver. Run: python -m relay.selfimprove.test_l2

Hermetic: NEVER calls the real loop.validate (which would launch real solves), NEVER runs git, no
network. The real validate is replaced by a stub via run_iteration's validate_fn= parameter, and
frozen_intact is monkeypatched so the frozen-set check is deterministic. Sentinel + archive are
backed by temp files.
"""
import json
import os
import tempfile

from relay.selfimprove import l2 as L2
from relay.selfimprove import frozen as F


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------

def _stub_validate(report):
    """Return a validate_fn that ignores its args and yields a canned report (or None)."""
    def _fn(**kwargs):
        return report
    return _fn


def _report(*, keep, verdict="keep", on=120, off=110, n=200):
    """A canned loop.validate-shaped report: plan keys + counts + a gate dict."""
    gate = {"keep": keep, "verdict": verdict, "reason": "stub gate reason",
            "p": 0.01 if keep else 0.4, "net_pp": 5.0, "n": n, "b": 30, "c": 10}
    return {"toggle": "SWE_TEST_KNOB", "n": n, "dataset": "Verified", "alpha": 0.05,
            "min_n": 100, "min_pp": 1.0, "targets_file": "x.txt",
            "on_resolved": on, "off_resolved": off, "gate": gate}


def _patch_frozen(monkey_ok, changed=None):
    """Replace F.frozen_intact (the symbol l2 calls through) with a fixed result; return a restorer."""
    orig = F.frozen_intact
    F.frozen_intact = lambda *a, **k: (monkey_ok, changed or [])
    return orig


def _write_sentinel(path, members, baseline):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"instance_ids": members, "baseline_resolved": baseline, "note": "t"}) + "\n")


# --------------------------------------------------------------------------------------------------
# (a) frozen-changed -> abort, and validate is NEVER called
# --------------------------------------------------------------------------------------------------

def test_frozen_changed_aborts():
    orig = _patch_frozen(False, ["relay/selfimprove/guards.py"])
    try:
        called = {"n": 0}

        def boom(**kwargs):
            called["n"] += 1
            raise AssertionError("validate must NOT run when the frozen set changed")

        with tempfile.TemporaryDirectory() as d:
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=boom)
        assert res["status"] == "abort", res
        assert "frozen set changed" in res["reason"]
        assert "guards.py" in res["reason"]
        assert res["frozen_ok"] is False
        assert res["final_keep"] is False
        assert called["n"] == 0
    finally:
        F.frozen_intact = orig
    print("ok test_frozen_changed_aborts")


# --------------------------------------------------------------------------------------------------
# (b) happy path: gate keep + frozen ok + no sentinel + auto_commit False -> queued
# --------------------------------------------------------------------------------------------------

def test_happy_path_queued():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            apath = os.path.join(d, "a.jsonl")
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200, auto_commit=False,
                                   archive_path=apath,
                                   validate_fn=_stub_validate(_report(keep=True)))
            assert res["status"] == "queued", res
            assert res["final_keep"] is True
            assert res["frozen_ok"] is True
            assert res["sentinel"] is None
            assert res["gate"]["keep"] is True
            assert any("sentinel skipped" in nt for nt in res["notes"])
            assert res["report"]["on_resolved"] == 120
            # archive recorded exactly one genome
            with open(apath, encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["pass_at_1"] == 120 and entry["gate_verdict"] == "keep"
            assert entry["genome"]["knobs"] == {"SWE_TEST_KNOB": "1"}
    finally:
        F.frozen_intact = orig
    print("ok test_happy_path_queued")


# --------------------------------------------------------------------------------------------------
# (c) gate keep but sentinel regressed -> rejected (the reward-hacking tripwire)
# --------------------------------------------------------------------------------------------------

def test_sentinel_regression_rejects():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            spath = os.path.join(d, "sentinel.json")
            # baseline resolves s1,s2,s3; candidate (on_resolved_ids) drops s2 -> regression
            _write_sentinel(spath, ["s1", "s2", "s3"], ["s1", "s2", "s3"])
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   sentinel_path=spath,
                                   on_resolved_ids=["s1", "s3"],
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(_report(keep=True)))
        assert res["status"] == "rejected", res
        assert res["final_keep"] is False
        assert res["sentinel"] is not None
        assert res["sentinel"]["regressed"] is True
        assert "s2" in res["sentinel"]["lost"]
        assert "REGRESSED" in res["reason"]
    finally:
        F.frozen_intact = orig
    print("ok test_sentinel_regression_rejects")


def test_sentinel_held_queued():
    """Sentinel configured + candidate holds the baseline -> still keep (queued)."""
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            spath = os.path.join(d, "sentinel.json")
            _write_sentinel(spath, ["s1", "s2", "s3"], ["s1", "s2"])
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   sentinel_path=spath,
                                   on_resolved_ids=["s1", "s2", "s3"],   # gained s3, lost none
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(_report(keep=True)))
        assert res["status"] == "queued", res
        assert res["sentinel"]["regressed"] is False
        assert "s3" in res["sentinel"]["gained"]
    finally:
        F.frozen_intact = orig
    print("ok test_sentinel_held_queued")


# --------------------------------------------------------------------------------------------------
# (d) auto_commit True + keep -> commit_pending (no git is ever run)
# --------------------------------------------------------------------------------------------------

def test_auto_commit_pending():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200, auto_commit=True,
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(_report(keep=True)))
        assert res["status"] == "commit_pending", res
        assert res["final_keep"] is True
        assert "not wired" in res["reason"]
    finally:
        F.frozen_intact = orig
    print("ok test_auto_commit_pending")


def test_validate_none_is_error():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(None))
        assert res["status"] == "error", res
        assert res["reason"] == "validate returned None"
    finally:
        F.frozen_intact = orig
    print("ok test_validate_none_is_error")


def test_gate_reject_no_sentinel():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(_report(keep=False, verdict="suggestive")))
        assert res["status"] == "rejected", res
        assert res["final_keep"] is False
    finally:
        F.frozen_intact = orig
    print("ok test_gate_reject_no_sentinel")


def test_frozen_post_change_aborts():
    """Frozen ok before the run, changed after -> abort_post, no keep/commit."""
    orig = F.frozen_intact
    seq = [(True, []), (False, ["bench/swe_grade_swebench.py"])]
    F.frozen_intact = lambda *a, **k: seq.pop(0)
    try:
        with tempfile.TemporaryDirectory() as d:
            res = L2.run_iteration(toggle="SWE_TEST_KNOB", n=200,
                                   archive_path=os.path.join(d, "a.jsonl"),
                                   validate_fn=_stub_validate(_report(keep=True)))
        assert res["status"] == "abort_post", res
        assert res["frozen_ok"] is False
        assert res["final_keep"] is False
        assert "during run" in res["reason"]
    finally:
        F.frozen_intact = orig
    print("ok test_frozen_post_change_aborts")


# --------------------------------------------------------------------------------------------------
# (e) SpendCeiling boundaries
# --------------------------------------------------------------------------------------------------

def test_spend_ceiling():
    sc = L2.SpendCeiling(start_ts=1000.0)
    # no iters, no time -> not exceeded
    assert sc.exceeded(max_iters=3, max_hours=2.0, now_ts=1000.0) is False
    sc.tick(); sc.tick()
    assert sc.iters == 2
    assert sc.exceeded(max_iters=3, max_hours=None, now_ts=1000.0) is False   # 2 < 3
    sc.tick()
    assert sc.exceeded(max_iters=3, max_hours=None, now_ts=1000.0) is True    # 3 >= 3 (inclusive)
    # time bound: 2h = 7200s; exactly at the boundary counts as exceeded
    sc2 = L2.SpendCeiling(start_ts=1000.0)
    assert sc2.exceeded(max_iters=None, max_hours=2.0, now_ts=1000.0 + 7199) is False
    assert sc2.exceeded(max_iters=None, max_hours=2.0, now_ts=1000.0 + 7200) is True
    # both None -> never exceeded
    assert sc2.exceeded(max_iters=None, max_hours=None, now_ts=1e12) is False
    # clock skew (now < start) reads zero elapsed, not negative
    assert sc2.elapsed_hours(500.0) == 0.0
    print("ok test_spend_ceiling")


def test_run_until_respects_ceiling():
    sc = L2.SpendCeiling(start_ts=0.0)
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        sc.tick()
        return {"step": calls["n"]}

    res = L2.run_until(lambda: sc.exceeded(max_iters=3, max_hours=None, now_ts=0.0), step)
    assert len(res) == 3 and calls["n"] == 3, res
    # already at the ceiling -> zero steps
    res2 = L2.run_until(lambda: sc.exceeded(max_iters=3, max_hours=None, now_ts=0.0), step)
    assert res2 == []
    print("ok test_run_until_respects_ceiling")


if __name__ == "__main__":
    test_frozen_changed_aborts()
    test_happy_path_queued()
    test_sentinel_regression_rejects()
    test_sentinel_held_queued()
    test_auto_commit_pending()
    test_validate_none_is_error()
    test_gate_reject_no_sentinel()
    test_frozen_post_change_aborts()
    test_spend_ceiling()
    test_run_until_respects_ceiling()
    print("ALL L2 TESTS PASSED")
