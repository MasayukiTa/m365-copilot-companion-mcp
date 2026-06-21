"""Unit tests for the L3 campaign policy. Run: python -m relay.selfimprove.test_policy

Hermetic: spec/burned files live in a tempdir; iterate_fn is a stub; now_fn is a fake clock. No
network, no wall clock, no L2 import.
"""
import json
import os
import tempfile

from relay.selfimprove import guards as G
from relay.selfimprove import policy as P


def _write_spec(path, ids):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{"instance_id": i} for i in ids], f)


# --------------------------------------------------------------------------------------------------
# 1. DatasetRotation
# --------------------------------------------------------------------------------------------------

def test_rotation_advances_and_exhausts():
    with tempfile.TemporaryDirectory() as d:
        spec_a = os.path.join(d, "a.json")
        spec_b = os.path.join(d, "b.json")
        _write_spec(spec_a, ["a__a-2", "a__a-1"])          # unsorted on purpose
        _write_spec(spec_b, ["b__b-1", "b__b-2"])
        burned = G.BurnedRegistry(os.path.join(d, "burned.jsonl"))
        rot = P.DatasetRotation([
            {"key": "A", "spec_path": spec_a},
            {"key": "B", "spec_path": spec_b},
            {"key": "Missing", "spec_path": os.path.join(d, "nope.json")},
        ])

        # first dataset has fresh ids -> deterministic sorted slice of up to n
        key, ids = rot.next_slice(2, burned, P._default_available_ids)
        assert key == "A" and ids == ["a__a-1", "a__a-2"]   # sorted
        key, ids = rot.next_slice(1, burned, P._default_available_ids)
        assert key == "A" and ids == ["a__a-1"]             # truncated to n

        # burn all of A -> rotation advances PAST the exhausted dataset to B
        burned.add(["a__a-1", "a__a-2"], reason="ab")
        key, ids = rot.next_slice(5, burned, P._default_available_ids)
        assert key == "B" and ids == ["b__b-1", "b__b-2"]

        # burn all of B -> every dataset exhausted (missing spec -> []) -> None
        burned.add(["b__b-1", "b__b-2"], reason="ab")
        assert rot.next_slice(5, burned, P._default_available_ids) is None

    print("ok test_rotation_advances_and_exhausts")


def test_rotation_default_reader_and_missing_file():
    with tempfile.TemporaryDirectory() as d:
        spec = os.path.join(d, "s.json")
        _write_spec(spec, ["x__x-9", "x__x-8"])
        burned = G.BurnedRegistry(os.path.join(d, "burned.jsonl"))
        rot = P.DatasetRotation([{"key": "X", "spec_path": spec}])
        # default available_ids_fn (None passed) reads the spec
        key, ids = rot.next_slice(10, burned)
        assert key == "X" and ids == ["x__x-8", "x__x-9"]
        # missing file -> [] -> exhausted
        assert P._default_available_ids(os.path.join(d, "absent.json")) == []
    print("ok test_rotation_default_reader_and_missing_file")


# --------------------------------------------------------------------------------------------------
# 2. plateaued
# --------------------------------------------------------------------------------------------------

def test_plateaued_boundary():
    F = {"kept": False}
    T = {"kept": True}
    # k-1 falses -> not plateaued; k falses -> plateaued
    assert P.plateaued([F, F], 3) is False
    assert P.plateaued([F, F, F], 3) is True
    # a True inside the last-k window resets it
    assert P.plateaued([F, T, F], 3) is False
    assert P.plateaued([T, F, F, F], 3) is True            # only last 3 matter, all false
    # final_keep takes precedence over kept; True there resets
    assert P.plateaued([{"final_keep": True}, F, F], 3) is False
    assert P.plateaued([{"final_keep": False}, F, F], 3) is True
    # too few results / k<=0 never plateau
    assert P.plateaued([F], 3) is False
    assert P.plateaued([F, F, F], 0) is False
    print("ok test_plateaued_boundary")


# --------------------------------------------------------------------------------------------------
# 3. tripwire predicates + evaluate_tripwires
# --------------------------------------------------------------------------------------------------

def test_tripwire_predicates_boundaries():
    # frozen changed
    assert P.tw_frozen_changed(False) is True
    assert P.tw_frozen_changed(True) is False
    # implausible jump: > max_pp fires; == does not; prev None never fires
    assert P.tw_implausible_jump(50.0, 75.0, max_pp=25.0) is False   # exactly 25 -> not > 25
    assert P.tw_implausible_jump(50.0, 75.1, max_pp=25.0) is True
    assert P.tw_implausible_jump(None, 99.0) is False
    assert P.tw_implausible_jump(60.0, 50.0) is False                # a drop never fires
    # infra spike: > threshold fires; == does not
    assert P.tw_infra_spike(0.30, threshold=0.30) is False
    assert P.tw_infra_spike(0.31, threshold=0.30) is True
    # sentinel
    assert P.tw_sentinel_regressed(True) is True
    assert P.tw_sentinel_regressed(False) is False
    print("ok test_tripwire_predicates_boundaries")


def test_evaluate_tripwires_present_only():
    # empty state -> nothing evaluated
    assert P.evaluate_tripwires({}) == []
    # only frozen present and fine -> []
    assert P.evaluate_tripwires({"frozen_ok": True}) == []
    # frozen broken fires; infra absent so NOT evaluated even though it would fire if present-default
    assert P.evaluate_tripwires({"frozen_ok": False}) == ["frozen_changed"]
    # new_pass present, prev None -> implausible not fired (first measurement)
    assert P.evaluate_tripwires({"new_pass": 90.0}) == []
    # multiple fired, order = frozen, jump, infra, sentinel
    fired = P.evaluate_tripwires({
        "frozen_ok": False, "prev_pass": 40.0, "new_pass": 80.0,
        "infra_rate": 0.5, "sentinel_regressed": True,
    })
    assert fired == ["frozen_changed", "implausible_jump", "infra_spike", "sentinel_regressed"]
    # present-but-not-firing inputs contribute nothing
    assert P.evaluate_tripwires({"infra_rate": 0.1, "sentinel_regressed": False}) == []
    print("ok test_evaluate_tripwires_present_only")


# --------------------------------------------------------------------------------------------------
# 4. run_campaign stop reasons (stub iterate_fn, fake now_fn)
# --------------------------------------------------------------------------------------------------

def _rotation_with(d, ids):
    spec = os.path.join(d, "camp.json")
    _write_spec(spec, ids)
    burned = G.BurnedRegistry(os.path.join(d, "burned.jsonl"))
    rot = P.DatasetRotation([{"key": "C", "spec_path": spec}])
    return rot, burned


def test_campaign_exhausted():
    with tempfile.TemporaryDirectory() as d:
        # only one fresh id; the stub burns it each iter so the pool empties -> exhausted
        rot, burned = _rotation_with(d, ["c__c-1"])

        def iterate_fn(key, slice_ids):
            burned.add(slice_ids, reason="ab")
            return {"kept": False}

        out = P.run_campaign(iterate_fn, rot, burned, n=5, max_iters=10)
        assert out["stop_reason"] == "exhausted"
        assert out["iterations"] == 1            # one productive iter, then pool empty
    print("ok test_campaign_exhausted")


def test_campaign_tripwire():
    with tempfile.TemporaryDirectory() as d:
        rot, burned = _rotation_with(d, ["c__c-1", "c__c-2", "c__c-3"])
        calls = {"n": 0}

        def iterate_fn(key, slice_ids):
            calls["n"] += 1
            burned.add(slice_ids[:1], reason="ab")
            if calls["n"] == 2:
                return {"kept": True, "frozen_ok": False}   # frozen tripwire fires on iter 2
            return {"kept": True}

        out = P.run_campaign(iterate_fn, rot, burned, n=1, max_iters=10)
        assert out["stop_reason"] == "tripwire:frozen_changed"
        # iter 2 is NOT recorded as progress (break before append)
        assert out["iterations"] == 1
        assert out["kept_ids"] == ["c__c-1"]
    print("ok test_campaign_tripwire")


def test_campaign_tripwire_from_result_list():
    with tempfile.TemporaryDirectory() as d:
        rot, burned = _rotation_with(d, ["c__c-1"])

        def iterate_fn(key, slice_ids):
            # explicit tripwires list (e.g. allowlist-violation the runner can't recompute)
            return {"kept": True, "tripwires": ["scaffold_allowlist_violation"]}

        out = P.run_campaign(iterate_fn, rot, burned, n=1, max_iters=5)
        assert out["stop_reason"] == "tripwire:scaffold_allowlist_violation"
        assert out["iterations"] == 0
    print("ok test_campaign_tripwire_from_result_list")


def test_campaign_plateau():
    with tempfile.TemporaryDirectory() as d:
        rot, burned = _rotation_with(d, ["c__c-%d" % i for i in range(20)])

        def iterate_fn(key, slice_ids):
            burned.add(slice_ids, reason="ab")
            return {"kept": False}                # never keeps -> plateau after k

        out = P.run_campaign(iterate_fn, rot, burned, n=1, max_iters=100, plateau_k=3)
        assert out["stop_reason"] == "plateau"
        assert out["iterations"] == 3
        assert out["kept_ids"] == []
    print("ok test_campaign_plateau")


def test_campaign_ceiling():
    with tempfile.TemporaryDirectory() as d:
        rot, burned = _rotation_with(d, ["c__c-%d" % i for i in range(50)])

        def iterate_fn(key, slice_ids):
            burned.add(slice_ids, reason="ab")
            return {"kept": True}                 # always keeps -> never plateaus

        out = P.run_campaign(iterate_fn, rot, burned, n=1, max_iters=4, plateau_k=3)
        assert out["stop_reason"] == "ceiling"
        assert out["iterations"] == 4
        assert len(out["kept_ids"]) == 4
    print("ok test_campaign_ceiling")


def test_campaign_time_ceiling():
    with tempfile.TemporaryDirectory() as d:
        rot, burned = _rotation_with(d, ["c__c-%d" % i for i in range(50)])
        clock = {"t": 1000.0}

        def now_fn():
            return clock["t"]

        def iterate_fn(key, slice_ids):
            burned.add(slice_ids, reason="ab")
            clock["t"] += 3600.0                  # one hour per iteration
            return {"kept": True}                 # keeps -> only time stops it

        out = P.run_campaign(iterate_fn, rot, burned, n=1, max_iters=100, plateau_k=999,
                             max_hours=2.0, now_fn=now_fn)
        assert out["stop_reason"] == "time_ceiling"
        assert out["iterations"] == 2             # 2h elapsed after 2 iters
    print("ok test_campaign_time_ceiling")


if __name__ == "__main__":
    test_rotation_advances_and_exhausts()
    test_rotation_default_reader_and_missing_file()
    test_plateaued_boundary()
    test_tripwire_predicates_boundaries()
    test_evaluate_tripwires_present_only()
    test_campaign_exhausted()
    test_campaign_tripwire()
    test_campaign_tripwire_from_result_list()
    test_campaign_plateau()
    test_campaign_ceiling()
    test_campaign_time_ceiling()
    print("ALL POLICY TESTS PASSED")
