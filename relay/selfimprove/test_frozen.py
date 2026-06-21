"""Unit tests for the frozen-constitution guard. Run: python -m relay.selfimprove.test_frozen"""
import os
import tempfile

from relay.selfimprove import frozen as F

# A small fake manifest used inside the temp repo (independent of the real FROZEN_MANIFEST).
FAKE_MANIFEST = ["a/grader.py", "b/guards.py", "c/constitution.md"]


def _make_repo(d: str) -> None:
    for rel in FAKE_MANIFEST:
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write("original content of %s\n" % rel)


def test_no_baseline():
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "frozen_baseline.json")
        ok, changed = F.frozen_intact(d, bp)
        assert ok is False and changed == ["NO_BASELINE"]
    print("ok test_no_baseline")


def test_snapshot_and_intact():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        # snapshot using the fake manifest, then verify intact
        sums = F.compute_checksums(d, FAKE_MANIFEST)
        data = {"repo_root": d, "checksums": sums}
        import json
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f)
        assert all(v != F.MISSING for v in sums.values())
        ok, changed = F.frozen_intact(d, bp)
        assert ok is True and changed == []
    print("ok test_snapshot_and_intact")


def test_modified_file_detected():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        import json
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"repo_root": d, "checksums": F.compute_checksums(d, FAKE_MANIFEST)}, f)
        # tamper with the guards file -- a reward-hack attempt
        with open(os.path.join(d, "b/guards.py"), "a", encoding="utf-8", newline="\n") as f:
            f.write("# always keep\n")
        ok, changed = F.frozen_intact(d, bp)
        assert ok is False
        assert "b/guards.py" in changed and "a/grader.py" not in changed
    print("ok test_modified_file_detected")


def test_deleted_file_detected():
    with tempfile.TemporaryDirectory() as d:
        _make_repo(d)
        bp = os.path.join(d, "frozen_baseline.json")
        import json
        with open(bp, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"repo_root": d, "checksums": F.compute_checksums(d, FAKE_MANIFEST)}, f)
        os.remove(os.path.join(d, "c/constitution.md"))
        ok, changed = F.frozen_intact(d, bp)
        assert ok is False and "c/constitution.md" in changed
        # the now-missing file reads as MISSING
        assert F.compute_checksums(d, ["c/constitution.md"])["c/constitution.md"] == F.MISSING
    print("ok test_deleted_file_detected")


def test_burned_append_only():
    old = ['{"instance_id": "a__a-1"}', '{"instance_id": "b__b-2"}']
    extended = old + ['{"instance_id": "c__c-3"}']
    rewritten = ['{"instance_id": "x__x-9"}', '{"instance_id": "b__b-2"}']
    shrunk = old[:1]
    assert F.burned_append_only(old, extended) is True
    assert F.burned_append_only(old, old) is True          # no change is fine
    assert F.burned_append_only(old, rewritten) is False    # first line changed
    assert F.burned_append_only(old, shrunk) is False       # ledger shrank
    print("ok test_burned_append_only")


def test_real_manifest_snapshot_roundtrip():
    # exercise snapshot_baseline / load_baseline against the real repo (read-only, temp baseline)
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "frozen_baseline.json")
        data = F.snapshot_baseline(F.REPO, bp)
        assert set(data["checksums"].keys()) == set(F.FROZEN_MANIFEST)
        loaded = F.load_baseline(bp)
        assert loaded == data
        ok, changed = F.frozen_intact(F.REPO, bp)
        assert ok is True and changed == []
    print("ok test_real_manifest_snapshot_roundtrip")


if __name__ == "__main__":
    test_no_baseline()
    test_snapshot_and_intact()
    test_modified_file_detected()
    test_deleted_file_detected()
    test_burned_append_only()
    test_real_manifest_snapshot_roundtrip()
    print("ALL FROZEN TESTS PASSED")
