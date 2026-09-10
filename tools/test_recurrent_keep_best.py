# -*- coding: utf-8 -*-
"""keep-best / rollback for the recurrent loop (codex-plan item A).

WHY THIS WAITED, AND WHAT RELEASED IT. recurrent_ops' own docstring deferred keep-best with a
reason: "on a verifier that reports pass/fail without a count, no round can be better than
another". That reason has not gone away -- it has a boundary. `bench/eval_one.py` prints OK or
HIDDEN_TESTS_FAILED and withholds the number ON PURPOSE, so a solver cannot hill-climb hidden
tests. Ordinary pytest output does report "3 failed", and there a round that lowers the count is
real progress that the loop used to revert along with everything else.

So the feature is not "rank the rounds". It is "rank them where a count legitimately exists, and
refuse to rank where it does not". test_a_round_without_a_count_is_never_the_best is the test
that keeps that boundary honest: without it, this file would be an optimiser pointed at hidden
tests, which is the benchmark overfitting this project forbids.

Nothing here touches a browser, a network, or the fleet. The verification command is replaced by
a stub, so a "round" is a file write plus a canned verifier output.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import recurrent_ops as R          # noqa: E402
from tools.auto import autoloop               # noqa: E402


# ---------------------------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_unlock(monkeypatch):
    """The gate is not what these tests are about; every one of them would otherwise stop at it."""
    monkeypatch.setattr(R, "require_unlocked", lambda: None)
    monkeypatch.setattr(autoloop, "stop_check", lambda: "RUN")


@pytest.fixture
def runlog(tmp_path, monkeypatch):
    """Point the runlog at a temp dir so a test never writes into the real .fleet."""
    root = tmp_path / "runlogs"
    root.mkdir()

    def _run_path(run_id):
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError("bad run_id")
        return root / (run_id + ".jsonl")

    def _append(run_id, rec):
        with open(str(_run_path(run_id)), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return ""

    monkeypatch.setattr(R, "_run_path", _run_path)
    monkeypatch.setattr(R, "runlog_append_local", _append)
    return root


class _Verifier:
    """Canned verification outputs, one per round, so a round's 'result' is deterministic."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def install(self, monkeypatch, repo):
        real_apply = autoloop.multi_edit_local if hasattr(autoloop, "multi_edit_local") else None

        def _edit_and_verify(edits, verify=None, repo=repo, run_id="", revert_on_fail=True,
                             timeout_s=None):
            """Apply the edits for real, then report the canned verifier output.

            Applying for real matters: the whole feature is about what is left on disk, so a
            stub that skipped the write would test nothing.
            """
            self.calls += 1
            # REAL PRE-IMAGES, like the function this stands in for. The first cut of this stub
            # reverted by writing e["old"], which is the text the CALLER believed was there --
            # not what was actually on disk. With keep_best off that produced a revert to the
            # wrong round and a false failure, and the fake was wrong, not the code.
            pre = {}
            for e in edits:
                p = os.path.join(repo, e["path"])
                pre[p] = open(p, encoding="utf-8").read() if os.path.exists(p) else None
            for e in edits:
                p = os.path.join(repo, e["path"])
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(e["new"])
            out = self.outputs[self.calls - 1]
            ok = "passed" in out and "failed" not in out
            res = {"ok": ok, "stage": "verify", "applied": [e["path"] for e in edits],
                   "reverted": False, "files": [], "check": "OK", "exit_code": 0 if ok else 1,
                   "output": out, "restore_failed": [], "stopped": False}
            if not ok and revert_on_fail:
                for p, text in pre.items():
                    if text is None:
                        if os.path.exists(p):
                            os.remove(p)
                    else:
                        with open(p, "w", encoding="utf-8") as fh:
                            fh.write(text)
                res["reverted"] = True
            return res

        monkeypatch.setattr(autoloop, "edit_and_verify", _edit_and_verify)
        return real_apply


def _begin(tmp_path, run_id, keep_best, max_iter=6, patience=9):
    return R.recurrent_begin("lower the failure count", verify_command="", repo=str(tmp_path),
                             run_id=run_id, max_iter=max_iter, patience=patience,
                             keep_best=keep_best)


def _edit(name, old, new):
    return [{"path": name, "old": old, "new": new}]


def _seed(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------------------------
# the feature
# ---------------------------------------------------------------------------------------------

def test_a_round_that_lowers_the_count_is_kept_on_disk(tmp_path, runlog, monkeypatch):
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["10 failed, 2 passed", "3 failed, 9 passed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb1", keep_best=True)

    R.recurrent_step("kb1", _edit("a.txt", "v0", "v1"))
    assert f.read_text(encoding="utf-8") == "v1", "the first counted round is the best so far"

    R.recurrent_step("kb1", _edit("a.txt", "v1", "v2"))
    assert f.read_text(encoding="utf-8") == "v2", (
        "10 -> 3 failures is progress; reverting it is what threw progress away before")

    state = json.loads(R.recurrent_state("kb1"))
    assert state["best_failures"] == 3
    assert state["best_iteration"] == 2
    assert state["standing_on"] == 2, "the tree carries round 2's edits and must say so"


def test_a_round_that_does_not_lower_the_count_is_rolled_back(tmp_path, runlog, monkeypatch):
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["3 failed, 9 passed", "7 failed, 5 passed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb2", keep_best=True)

    R.recurrent_step("kb2", _edit("a.txt", "v0", "v1"))
    R.recurrent_step("kb2", _edit("a.txt", "v1", "v2"))

    assert f.read_text(encoding="utf-8") == "v1", (
        "a round that made things worse must leave the best round standing, not itself")
    state = json.loads(R.recurrent_state("kb2"))
    assert state["standing_on"] == 1
    assert state["best_iteration"] == 1
    assert state["history"][1]["kept"] is False


def test_a_round_without_a_count_is_never_the_best(tmp_path, runlog, monkeypatch):
    """THE BOUNDARY. bench/eval_one.py withholds the number so nobody can hill-climb hidden
    tests. A round whose count is None must therefore never be kept, however plausible it
    looks -- ranking there would be exactly the overfitting this project forbids."""
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["HIDDEN_TESTS_FAILED", "HIDDEN_TESTS_FAILED"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb3", keep_best=True)

    R.recurrent_step("kb3", _edit("a.txt", "v0", "v1"))
    R.recurrent_step("kb3", _edit("a.txt", "v1", "v2"))

    assert f.read_text(encoding="utf-8") == "v0", (
        "a sealed verifier gives no gradient, so nothing may be kept on the strength of one")
    state = json.loads(R.recurrent_state("kb3"))
    assert state["best_iteration"] is None
    assert state["standing_on"] == R.STANDING_ON_START
    assert state["rankable_rounds"] == 0, (
        "the state must distinguish 'no round was better' from 'no round could be compared'")
    assert state["history"][0]["kept"] is False
    recs = [json.loads(l) for l in open(str(runlog / "kb3.jsonl"), encoding="utf-8") if l.strip()]
    unkept = [r for r in recs if r.get("kind") == R.KIND_ROUND]
    assert unkept[0]["not_kept_because"] == "no comparable failure count"


def test_ties_keep_the_earlier_round(tmp_path, runlog, monkeypatch):
    """Two rounds at the same count are not equally good to keep: the earlier one is already
    banked, and churning the tree to the later one buys nothing."""
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["4 failed, 1 passed", "4 failed, 1 passed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb4", keep_best=True)

    R.recurrent_step("kb4", _edit("a.txt", "v0", "v1"))
    R.recurrent_step("kb4", _edit("a.txt", "v1", "v2"))

    assert f.read_text(encoding="utf-8") == "v1"
    assert json.loads(R.recurrent_state("kb4"))["best_iteration"] == 1


def test_keep_best_off_leaves_the_old_behaviour_alone(tmp_path, runlog, monkeypatch):
    """The default must not change. Off, every failing round is reverted by edit_and_verify
    itself, exactly as before, and the tree ends where it started."""
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["10 failed, 2 passed", "3 failed, 9 passed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb5", keep_best=False)

    R.recurrent_step("kb5", _edit("a.txt", "v0", "v1"))
    R.recurrent_step("kb5", _edit("a.txt", "v1", "v2"))

    assert f.read_text(encoding="utf-8") == "v0", "keep_best defaults off; nothing may be kept"
    state = json.loads(R.recurrent_state("kb5"))
    assert state["keep_best"] is False
    assert state["standing_on"] == R.STANDING_ON_START


def test_a_passing_round_still_converges_and_stands(tmp_path, runlog, monkeypatch):
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["12 passed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb6", keep_best=True)

    out = json.loads(R.recurrent_step("kb6", _edit("a.txt", "v0", "v1")))
    assert out["stop"] == "converged"
    assert f.read_text(encoding="utf-8") == "v1"
    assert out["standing_on"] == 1


def test_a_round_whose_pre_image_cannot_be_read_is_refused_before_writing(tmp_path, runlog,
                                                                          monkeypatch):
    """A round that could not be undone must not be started. edit_and_verify already returns
    before its first write when a pre-image is unreadable; keeping a round means this loop owns
    the undo, so it owes the same guarantee."""
    f = _seed(tmp_path, "a.txt", "v0")
    _Verifier(["1 failed"]).install(monkeypatch, str(tmp_path))
    _begin(tmp_path, "kb7", keep_best=True)

    def _boom(_path):
        raise OSError("permission denied")
    monkeypatch.setattr(autoloop, "_read", _boom)

    out = json.loads(R.recurrent_step("kb7", _edit("a.txt", "v0", "v1")))

    assert out["stop"] == "stopped"
    assert "pre-image" in out["reason"] or "could not be undone" in out["reason"]
    assert f.read_text(encoding="utf-8") == "v0", "it wrote despite being unable to undo"


def test_the_countable_guard_rejects_a_boolean(tmp_path, runlog):
    """isinstance(True, int) is True in Python, so a round recorded as fails=True would rank as
    one failure and quietly become 'the best round'."""
    assert R._countable({"fails": 3}) is True
    assert R._countable({"fails": 0}) is True
    assert R._countable({"fails": None}) is False
    assert R._countable({"fails": True}) is False
    assert R._countable({}) is False
