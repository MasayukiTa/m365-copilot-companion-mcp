# -*- coding: utf-8 -*-
"""Nineteen ready worktrees were thrown away because one failed, and it cost a slice.

MEASURED 2026-09-12, staging chunk 1 of the OFF arm:

    --- done: 19 prepared, 1 failed ---
    FAILED: django__django-11532
    stage failed for chunk 1; skipping (will retry on resume)

Those nineteen were ready. Discarding them left the arm at 80/100, so the paired McNemar had
N=80 against min_n=100, the gate returned `underpowered`, and `burned.add(fresh)` then consumed
all 100 fresh instances for a verdict that says nothing.

WHY THE ONE FAILED, reproduced directly rather than inferred:

    $ cmd /c rmdir /s /q ...\\wt_django__django-11532
    $ git ... worktree add -f ...
    WORKTREE FAILED: fatal: '.../wt_django__django-11532' already exists

and the directory could not be removed at all:

    REMOVE FAILED: The process cannot access the file
    '...\\wt_django__django-11532\\tests' because it is being used by another process.

A single empty directory, held by a process that does not name it in its command line. It had
already failed this way twice -- and an earlier report of mine called it transient on the
grounds that the ON arm had staged it successfully. One success does not make a failure
non-deterministic.

THREE FAULTS, of which the second is the expensive one:

  (a) the removal's result is never checked, so the `worktree add` after it is a guaranteed
      failure whenever a stale directory survives
  (b) one instance failing discarded the nineteen that succeeded
  (c) capture() wrote a prediction for an instance with no worktree, so something never
      attempted was recorded as an empty patch -- which grades as `not resolved` and enters
      McNemar as evidence about the change under test

(c) has to be fixed alongside (b): once a partial chunk runs, the unstaged instance would
otherwise be recorded as a failure instead of being left for a later run to solve.
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench import swe_solve_decoupled as S  # noqa: E402


# ── (b) a partial stage runs what is ready ────────────────────────────────────────────────

def test_a_failed_stage_no_longer_skips_the_chunk():
    src = inspect.getsource(S.main)
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    i = body.index("if not stage(")
    after = body[i:i + 400]
    assert "continue" not in after.split("goals_path")[0], (
        "a failed stage still discards the whole chunk; nineteen ready worktrees would be "
        "thrown away for one failure, which is what cost a hundred-instance slice")


def test_nothing_staged_at_all_still_skips():
    """The guard keeps its purpose. write_goals returns 0 when no instance has a worktree, and
    that branch must still skip -- running a fleet with no goals is not an improvement."""
    src = inspect.getsource(S.main)
    assert "no goals written for chunk" in src
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    j = body.index("if ng == 0:")
    assert "continue" in body[j:j + 120]


def test_a_short_chunk_is_reported():
    """A silent 19-of-20 is how an arm quietly ends up at 80/100."""
    assert "deferred to a later run" in inspect.getsource(S.main)


# ── (c) an instance that never ran is not an empty answer ─────────────────────────────────

def test_capture_writes_nothing_for_an_unstaged_instance(tmp_path, monkeypatch):
    """THE ONE THAT WOULD HAVE POISONED THE GATE. An empty patch grades as `not resolved`, so
    an instance the harness never staged would have entered McNemar as evidence about the
    change instead of being left out as the infrastructure gap it is."""
    work = tmp_path / "work"
    preds = tmp_path / "preds"
    staged = work / "wt_repo__repo-1"
    staged.mkdir(parents=True)
    monkeypatch.setattr(S, "WORK", str(work))
    monkeypatch.setattr(S, "PREDS", str(preds))
    monkeypatch.setattr(S, "log", lambda *a, **k: None)
    monkeypatch.setattr(S, "_decode_child", lambda raw: "diff --git a/x b/x\n+1\n")

    nonempty, captured = S.capture(["repo__repo-1", "repo__repo-2"])

    assert os.path.isfile(str(preds / "repo__repo-1.json")), "the staged instance was not captured"
    assert not os.path.exists(str(preds / "repo__repo-2.json")), (
        "an instance with no worktree got a prediction; it will grade as a real failure")
    assert captured == 1 and nonempty == 1


def test_capture_reports_what_it_captured_not_the_chunk_size(tmp_path, monkeypatch):
    """The log line printed len(ch)/len(ch), so "captured 20/20" was true by construction."""
    work = tmp_path / "work"
    preds = tmp_path / "preds"
    (work / "wt_a__a-1").mkdir(parents=True)
    monkeypatch.setattr(S, "WORK", str(work))
    monkeypatch.setattr(S, "PREDS", str(preds))
    monkeypatch.setattr(S, "log", lambda *a, **k: None)
    monkeypatch.setattr(S, "_decode_child", lambda raw: "")

    nonempty, captured = S.capture(["a__a-1", "a__a-2", "a__a-3"])
    assert captured == 1, "capture must count what it actually wrote"
    assert nonempty == 0

    src = inspect.getsource(S.main)
    assert "(nc, len(ch), ne)" in src, "the log still reports the chunk size as the capture count"


def test_an_empty_diff_from_a_staged_worktree_is_still_captured(tmp_path, monkeypatch):
    """The distinction being drawn is 'never attempted' vs 'attempted and changed nothing'.
    The second is a real result and must keep its prediction, or a legitimately empty answer
    would silently leave the denominator."""
    work = tmp_path / "work"
    preds = tmp_path / "preds"
    (work / "wt_a__a-1").mkdir(parents=True)
    monkeypatch.setattr(S, "WORK", str(work))
    monkeypatch.setattr(S, "PREDS", str(preds))
    monkeypatch.setattr(S, "log", lambda *a, **k: None)
    monkeypatch.setattr(S, "_decode_child", lambda raw: "")

    nonempty, captured = S.capture(["a__a-1"])
    assert captured == 1 and nonempty == 0
    with open(str(preds / "a__a-1.json"), encoding="utf-8") as fh:
        assert json.load(fh)[0]["model_patch"] == ""
