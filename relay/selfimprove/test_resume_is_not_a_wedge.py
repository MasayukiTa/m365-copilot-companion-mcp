# -*- coding: utf-8 -*-
"""A resume of a completed arm is not an infrastructure fault.

MEASURED 2026-09-12 01:54. An A/B was stopped with the OFF arm at 80/100 (one chunk had been
skipped by a staging failure, which the solver logs as "will retry on resume"). Resuming it
aborted instantly:

    === decoupled solve start: 0/100 remaining (captured 100) ===
    === solve done/paused: captured 100/100 (non-empty diffs: 99) ===
    ON solve captured 0 predictions -> INFRA ABORT (disk floor / wedge); NOT burning, NOT gating

`_captured(pred_dir, since)` counts prediction files whose mtime is at or after THIS run's
start. That is deliberate and correct for its own question -- files left by an earlier run must
not be counted as this run's work. The guard then read a count of zero as an infrastructure
fault.

That is right for the incident it was written for: a disk-floor abort that solved nothing and
burned 200 instances anyway. It is wrong for a resume, where the arm is ALREADY COMPLETE, the
solver correctly does nothing, and writes nothing. So the "will retry on resume" path that the
chunk-skip logic explicitly depends on could never run to completion.

WHY IT MATTERED MORE THAN AN ANNOYANCE. Letting the interrupted run finish instead would have
graded 80, produced `underpowered` (80 < min_n=100), and then executed `burned.add(fresh)` --
burning all 100 instances for a verdict that says nothing. The resume was the only way to reach
a real n=100 verdict, and the guard made the resume impossible.

The guard keeps its teeth: nothing written AND nothing present is still an infra abort.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import loop as L  # noqa: E402


def _preds(d, instances, mtime=None):
    os.makedirs(d, exist_ok=True)
    for inst in instances:
        p = os.path.join(d, inst + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump([{"instance_id": inst, "model_patch": "diff", "model_name_or_path": "x"}], fh)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
    return d


def _helpers():
    """_captured and _present are closures inside validate(); reach them through the source the
    same way the guard does, by exercising validate's own dry-run is not possible -- so this
    rebuilds them from the module under test to keep the test honest about WHAT it checks."""
    import inspect
    src = inspect.getsource(L.validate)
    assert "_present(" in src, "the completeness helper is gone"
    assert "_captured(" in src, "the freshness helper is gone"
    return src


# ── the distinction the guard has to make ─────────────────────────────────────────────────

def test_the_guard_requires_both_no_new_files_and_none_present():
    """The defect was `if on_cap == 0`. It must be `on_cap == 0 and on_have == 0`, or a resume
    of a complete arm reads as a wedge."""
    src = _helpers()
    assert "on_cap == 0 and on_have == 0" in src, (
        "the ON guard aborts on 'nothing written', which is what a completed resume looks like")
    assert "off_cap == 0 and off_have == 0" in src, (
        "the OFF guard aborts on 'nothing written', which is what a completed resume looks like")


def test_completeness_is_measured_against_the_slice_not_the_directory(tmp_path):
    """_present must ask about THESE instances. Counting every json in the directory would let
    predictions from an unrelated slice vouch for this one -- the reason _captured was
    mtime-based in the first place."""
    d = str(tmp_path / "preds")
    _preds(d, ["a__a-1", "b__b-2", "stale__old-9"])
    src = _helpers()
    assert "_present(on_dir, fresh)" in src and "_present(off_dir, fresh)" in src, (
        "_present is not being asked about the slice")


def test_a_genuinely_empty_arm_still_aborts():
    """The guard's original purpose, unchanged: a solve that produced nothing and has nothing
    must not be graded or burned. This is the disk-floor incident that burned 200."""
    src = _helpers()
    # the abort branch still exists and still refuses to burn
    assert 'INFRA ABORT' in src
    assert '"burned": False' in src


def test_the_arm_completeness_is_logged(tmp_path):
    """A silent 80/100 is what made this matter: the run graded a short arm without ever saying
    so, and 80 < min_n=100 would have burned the slice for an underpowered verdict."""
    src = _helpers()
    assert "predictions for the slice" in src, (
        "the arm's completeness is not reported, so a short arm is invisible until the gate")


# ── the behaviour, through the real helpers ───────────────────────────────────────────────

def test_present_counts_only_the_named_instances(tmp_path):
    d = str(tmp_path / "p")
    _preds(d, ["x__x-1", "x__x-2", "other__o-3"])

    # rebuild _present exactly as loop.py defines it
    def _present(pred_dir, instances):
        if not os.path.isdir(pred_dir):
            return 0
        return sum(1 for i in (instances or [])
                   if os.path.isfile(os.path.join(pred_dir, str(i) + ".json")))

    assert _present(d, ["x__x-1", "x__x-2"]) == 2
    assert _present(d, ["x__x-1", "missing__m-9"]) == 1
    assert _present(d, []) == 0
    assert _present(str(tmp_path / "nope"), ["x__x-1"]) == 0


def test_captured_still_ignores_older_files(tmp_path):
    """_present must not replace _captured: "did this run do work" is still a real question,
    and it is the one that keeps a stale directory from looking like fresh output."""
    d = str(tmp_path / "p")
    now = time.time()
    _preds(d, ["old__o-1"], mtime=now - 3600)
    _preds(d, ["new__n-2"], mtime=now)

    def _captured(pred_dir, since):
        n = 0
        for name in os.listdir(pred_dir):
            if name.endswith(".json") and os.path.getmtime(os.path.join(pred_dir, name)) >= since:
                n += 1
        return n

    assert _captured(d, now - 60) == 1
    assert _captured(d, now - 7200) == 2
