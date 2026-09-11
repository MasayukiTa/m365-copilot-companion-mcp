# -*- coding: utf-8 -*-
"""supervisor_verify finally has a live caller (codex-plan item 1).

relay/supervisor_verify.py owns the DONE / CANDIDATE_DONE / VERIFY_FAILED / VERIFY_UNAVAILABLE
vocabulary, and its only non-test caller was bench/pro_cycle.py, in shadow. The live fleet has
never produced CANDIDATE_DONE or VERIFY_UNAVAILABLE.

WHAT IS AND IS NOT WIRED, AND WHY. supervisor_verify lists four steps. The live worker already
does 1-3: it stops, and it runs the CONTRACT's own commands (_advance_check -> Check(spec,
cwd=self.cwd)), setting verified=True only once every one drained. Re-running them at settle
would be a second implementation of a job already done, and verify() is synchronous -- a
SWE-bench acceptance command is a ~1300s docker eval, which would freeze the single-threaded
round-robin. That is why the whole module was never wired live, and re-running is the wrong
half to wire.

STEP 4 IS THE HALF THE LIVE PATH LACKS: hash the tree, and treat a green as void if the tree
moved afterwards. Concretely reachable here -- fanout children INHERIT the parent's cwd
(relay/fanout.py:140), so siblings work the same tree at the same time, and a sibling's write
after this worker's checks went green leaves a verdict describing a tree that no longer exists.

RECORDED, NOT GATED, and that is this repository's own rule rather than timidity.
evidence_manifest states it: "Nothing here changes an outcome; the caller records the verdict
beside the existing one so the two can be compared over a real run before anything is gated on
it." There are zero measurements of how often a live tree moves under a finished worker, and
gating would additionally mean introducing CANDIDATE_DONE into an outcome enum hand-mirrored in
five places.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as RF          # noqa: E402
from relay import supervisor_verify as SV    # noqa: E402

CHECKS = [{"type": "shell", "cmd": "pytest -q", "id": "acceptance"}]


def _worker(tmp_path, checks=CHECKS):
    goal = {"text": "fix the thing", "checks": checks, "cwd": str(tmp_path)}
    return RF.RelayWorker(goal, "w0")


def test_the_module_is_actually_called(tmp_path, monkeypatch):
    """The point of the item: supervisor_verify has a live caller now. Asserted by observing
    ITS function being used, not by grepping an import."""
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    calls = []
    real = SV.tree_hash
    monkeypatch.setattr(SV, "tree_hash", lambda root, **k: (calls.append(root), real(root))[1])

    w = _worker(tmp_path)
    w._pending_checks = []
    w._candidate_done = lambda: None          # stop before the refuter
    w._advance_check()

    assert calls == [str(tmp_path)], "supervisor_verify.tree_hash was not called"
    assert w._verified_tree, "the tree the green describes was not captured"


def test_a_tree_that_did_not_move_is_recorded_stable(tmp_path):
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    w._verified_tree = w._tree_hash_now()
    w._record_tree_stability()
    assert w.tree_stable is True


def test_a_sibling_write_after_the_green_is_noticed(tmp_path):
    """THE CASE THIS EXISTS FOR. Nothing this worker did changed; another worker sharing the
    cwd wrote, and the green now describes a tree that is gone."""
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    w._verified_tree = w._tree_hash_now()

    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")   # the sibling
    w._record_tree_stability()

    assert w.tree_stable is False


def test_it_does_not_change_the_outcome(tmp_path, monkeypatch):
    """SHADOW FIRST. A moved tree is recorded and the worker still settles exactly as before --
    switching a gate from permissive to closed without measuring first is a mistake this
    repository has already been corrected for."""
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    w._verified_tree = w._tree_hash_now()
    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")
    monkeypatch.setattr(w, "_claim_verdict", lambda: "DONE")

    w._settle_done()

    assert w.status == "done"
    assert w.outcome == "DONE", "a recorded observation was allowed to change the outcome"
    assert w.tree_stable is False, "...but it was still recorded"


def test_a_worker_with_no_cwd_asks_nothing(tmp_path):
    """Ordinary Copilot fleet goals carry no cwd at all (task_router builds a bare string), so
    this must be silent for them rather than hashing something arbitrary."""
    w = RF.RelayWorker("investigate the thing", "w0")
    assert w._tree_hash_now() == ""
    w._record_tree_stability()
    assert w.tree_stable is None, "None means the question was never asked, and it wasn't"


def test_a_worker_that_never_went_green_asks_nothing(tmp_path):
    """No green, no 'before' -- and a comparison against nothing must not read as stable."""
    w = _worker(tmp_path)
    assert w._verified_tree == ""
    w._record_tree_stability()
    assert w.tree_stable is None


def test_the_tri_state_is_a_tri_state(tmp_path, monkeypatch):
    """None / True / False mean never-asked / unchanged / moved -- the same discipline
    `verified` is built on, where collapsing None into False once made 67 unconfigured workers
    read as verification failures."""
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    assert w.tree_stable is None
    w._verified_tree = w._tree_hash_now()
    w._record_tree_stability()
    assert w.tree_stable is True
    # an unreadable tree is not a moved tree: it is a question that could not be answered
    monkeypatch.setattr(w, "_tree_hash_now", lambda: "")
    w._record_tree_stability()
    assert w.tree_stable is None


def test_measuring_can_never_fail_the_worker(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("hashing exploded")
    monkeypatch.setattr(SV, "tree_hash", _boom)
    w = _worker(tmp_path)
    assert w._tree_hash_now() == ""
    w._verified_tree = "deadbeef"
    w._record_tree_stability()                 # must not raise
    monkeypatch.setattr(w, "_claim_verdict", lambda: "DONE")
    w._settle_done()
    assert w.outcome == "DONE"


def test_the_hash_actually_distinguishes_trees(tmp_path):
    """Guards against the degenerate case that would make every assertion above vacuous: a
    tree_hash over a MISSING directory returns the digest of no input, which is non-empty and
    equal to itself, so an absent tree reads as 'stable'."""
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    h1 = SV.tree_hash(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")
    h2 = SV.tree_hash(str(tmp_path))
    assert h1 and h2 and h1 != h2
    missing = SV.tree_hash(str(tmp_path / "nope"))
    assert missing != h1


# ── the instrument has a reader, and a denominator ────────────────────────────────────────

def test_both_outcomes_are_recorded_so_a_rate_exists(tmp_path, monkeypatch):
    """A log holding only the positive cases can say a thing happened and never what share of
    the time. That is the distinction the staircase fields exist for, and this repository
    already paid for collapsing it once -- "the panel ran 155 times" could not be made a rate.
    """
    from relay import mechanism_telemetry as MT
    rows = []
    monkeypatch.setattr(MT, "record", lambda mech, **kw: rows.append((mech, kw)))

    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    w._verified_tree = w._tree_hash_now()
    w._record_tree_stability()                      # unchanged
    assert rows, "an unchanged tree recorded nothing, so there is no denominator"
    mech, kw = rows[-1]
    assert mech == "tree_moved_after_verify"
    assert kw["eligible"] is True and kw["triggered"] is False

    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")
    w._record_tree_stability()                      # moved
    _mech, kw2 = rows[-1]
    assert kw2["eligible"] is True and kw2["triggered"] is True


def test_an_unanswerable_question_is_not_a_no(tmp_path, monkeypatch):
    """eligible=False means the tree could not be re-read. Recording that as triggered=False
    would put "we could not tell" into the denominator as "it did not move"."""
    from relay import mechanism_telemetry as MT
    rows = []
    monkeypatch.setattr(MT, "record", lambda mech, **kw: rows.append((mech, kw)))
    w = _worker(tmp_path)
    w._verified_tree = "deadbeef"
    monkeypatch.setattr(w, "_tree_hash_now", lambda: "")
    w._record_tree_stability()
    _mech, kw = rows[-1]
    assert kw["eligible"] is False
    assert kw["ineligible_reason"]
    assert w.tree_stable is None


def test_the_mechanism_is_registered_so_summaries_can_see_it():
    """record() writes an unregistered mechanism deliberately, but summarise() walks
    MECHANISMS -- so an unregistered one is written and never read, which is the failure this
    whole item is about."""
    from relay import mechanism_telemetry as MT
    assert "tree_moved_after_verify" in MT.MECHANISMS


def test_nothing_was_acted_on_and_the_record_says_so(tmp_path, monkeypatch):
    """executed=False is not decoration: without it a reader has to assume a recorded finding
    changed something, and it deliberately does not."""
    from relay import mechanism_telemetry as MT
    rows = []
    monkeypatch.setattr(MT, "record", lambda mech, **kw: rows.append((mech, kw)))
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    w._verified_tree = w._tree_hash_now()
    (tmp_path / "a.py").write_text("x = 2", encoding="utf-8")
    w._record_tree_stability()
    assert rows[-1][1]["executed"] is False


def test_the_hash_declares_its_own_measured_bound_not_the_acceptance_ceiling(tmp_path):
    """It used to declare _eval_ceiling_s() -- at least 1500s -- for an operation measured at
    7.3s idle and 29.8s under load. A window fifty times the work is not a declaration, it is
    a blank cheque: if the hash ever wedged, the watchdog would vouch for the browser for
    twenty-five minutes before the failsafe caught up."""
    import time as _t
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    w = _worker(tmp_path)
    seen = {}

    def _spy(seconds):
        seen["s"] = seconds
        w.eval_busy_until = _t.time() + seconds
    w._declare_blocking = _spy

    w._tree_hash_now()

    assert seen.get("s") == RF.TREE_HASH_CEILING_S
    assert RF.TREE_HASH_CEILING_S < RF.EVAL_STALL_CEILING_S, (
        "the tree hash declares a window as wide as a full acceptance eval")
    assert RF.TREE_HASH_CEILING_S >= 30, (
        "the bound is under the 29.8s this was measured to take under load, so a normal hash "
        "would look like a wedge")


def test_the_window_closes_even_when_hashing_raises(tmp_path, monkeypatch):
    """A window left open would make this worker vouch for a browser it stopped watching."""
    def _boom(*a, **k):
        raise RuntimeError("hashing exploded")
    monkeypatch.setattr(SV, "tree_hash", _boom)
    w = _worker(tmp_path)
    w._tree_hash_now()
    assert w.eval_busy_until == 0.0
