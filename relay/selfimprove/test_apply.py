"""Unit tests for the genome applier + frozen-safe commit helper.

Hermetic: the store lives in a tempfile, frozen.frozen_intact is monkeypatched, and NO real git is
ever run -- only dry_run=True paths and the refusal branches are exercised.

Run: python -m relay.selfimprove.test_apply
"""
import json
import os
import tempfile

from relay.selfimprove import apply as A
from relay.selfimprove import frozen


def test_apply_and_read_back():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "active_genome.json")
        # no store yet -> empty base
        base = A.active_genome(store)
        assert base == {"knobs": {}, "cards": {}, "parent_id": None, "note": "base"}

        g = {"knobs": {"SS_X": "1"}, "cards": {"c1": "text"}, "parent_id": "p0", "note": "g1"}
        ret = A.apply_genome(g, store)
        assert ret == g
        assert os.path.isfile(store)
        # written as pretty json, trailing newline, no BOM
        with open(store, "rb") as f:
            raw = f.read()
        assert not raw.startswith(b"\xef\xbb\xbf")        # no BOM
        assert raw.endswith(b"\n")
        assert b"  " in raw                                # indented (pretty)
        # active_genome reads it back
        assert A.active_genome(store) == g
        assert json.loads(raw.decode("utf-8")) == g
    print("ok test_apply_and_read_back")


def test_apply_backup_and_revert():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "active_genome.json")
        prev = store + ".prev"

        g1 = {"knobs": {"A": "1"}, "cards": {}, "parent_id": None, "note": "g1"}
        g2 = {"knobs": {"A": "2"}, "cards": {}, "parent_id": "x", "note": "g2"}

        # first apply: no prior store, so no backup yet; revert finds nothing
        A.apply_genome(g1, store)
        assert not os.path.isfile(prev)
        assert A.revert(store) is False

        # second apply: backs up g1 to .prev, store becomes g2
        A.apply_genome(g2, store)
        assert os.path.isfile(prev)
        assert A.active_genome(store) == g2
        with open(prev, encoding="utf-8") as f:
            assert json.load(f) == g1

        # revert restores g1
        assert A.revert(store) is True
        assert A.active_genome(store) == g1
    print("ok test_apply_backup_and_revert")


def test_safe_commit_refuses_when_frozen_changed(monkeypatch):
    monkeypatch.setattr(frozen, "frozen_intact",
                        lambda repo=None, baseline=None: (False, ["relay/selfimprove/guards.py"]))
    res = A.safe_commit(["relay/quality_cards.py"], "msg")
    assert res["ok"] is False
    assert res["committed"] is False
    assert "frozen set changed" in res["reason"]
    print("ok test_safe_commit_refuses_when_frozen_changed")


def test_safe_commit_refuses_non_allowlisted(monkeypatch):
    monkeypatch.setattr(frozen, "frozen_intact", lambda repo=None, baseline=None: (True, []))
    # a path not in the scaffold allowlist
    res = A.safe_commit(["relay/main.py"], "msg")
    assert res["ok"] is False and res["committed"] is False
    assert "not in scaffold allowlist" in res["reason"]
    print("ok test_safe_commit_refuses_non_allowlisted")


def test_safe_commit_refuses_frozen_path(monkeypatch):
    monkeypatch.setattr(frozen, "frozen_intact", lambda repo=None, baseline=None: (True, []))
    # frozen.py is in FROZEN_MANIFEST -> forbidden even if someone listed it
    frozen_path = frozen.FROZEN_MANIFEST[0]
    assert frozen_path not in A.SCAFFOLD_ALLOWLIST  # sanity: not allowlisted anyway
    res = A.safe_commit([frozen_path], "msg")
    assert res["ok"] is False and res["committed"] is False
    print("ok test_safe_commit_refuses_frozen_path")


def test_safe_commit_dry_run_does_not_touch_git(monkeypatch):
    monkeypatch.setattr(frozen, "frozen_intact", lambda repo=None, baseline=None: (True, []))
    # blow up if git is ever invoked -- dry_run must not call subprocess
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("git must not run in dry_run")

    monkeypatch.setattr(subprocess, "run", _boom)

    paths = ["relay/selfimprove/active_genome.json", "relay/quality_cards.py"]
    res = A.safe_commit(paths, "apply g1")           # dry_run defaults True
    assert res["ok"] is True
    assert res["committed"] is False
    assert res["would_commit"] == paths
    assert res["message"] == "apply g1"
    print("ok test_safe_commit_dry_run_does_not_touch_git")


# --- minimal monkeypatch shim (stdlib-only; mirrors test_guards.py's no-pytest run) ---------------


class _MonkeyPatch:
    """Tiny setattr-only monkeypatch with automatic undo, so tests run under plain python."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo = []


def _run(fn):
    mp = _MonkeyPatch()
    try:
        fn(mp)
    finally:
        mp.undo()


if __name__ == "__main__":
    test_apply_and_read_back()
    test_apply_backup_and_revert()
    _run(test_safe_commit_refuses_when_frozen_changed)
    _run(test_safe_commit_refuses_non_allowlisted)
    _run(test_safe_commit_refuses_frozen_path)
    _run(test_safe_commit_dry_run_does_not_touch_git)
    print("ALL APPLY TESTS PASSED")
