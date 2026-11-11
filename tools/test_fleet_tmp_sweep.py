# -*- coding: utf-8 -*-
"""Stale "*.json.tmp" debris must not read as a pending backlog.

fleet_submit writes a job to "<jid>.json.tmp" and os.replace()s it onto "<jid>.json". The
rename is atomic, so a .tmp that is still present long after is the debris of a submitter that
died between open() and os.replace(). task_router claims only "*.json", so such a tmp is never
claimable and sits forever. On 2026-11-11 six 0-byte "*.json.tmp" (mtimes 11/02 16:51 x3,
11/07 11:49, 11/07 13:49, 11/11 05:52) made fleet_queue report "pending 6" with nothing to run.

These tests pin the fix on the cause side: fleet_queue counts only real task files, and the
sweep deletes tmp older than the cutoff while sparing young tmp and real "*.json" tasks.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """A queue of its own, so these tests never touch the real .fleet/tasks."""
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    TR.ensure_dirs()
    return tmp_path


def _pending_dir():
    return os.path.join(TR.TASKS, "pending")


def _write(name, content="", age_s=0.0):
    """Create a file in pending/ and optionally backdate its mtime by age_s seconds."""
    p = os.path.join(_pending_dir(), name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


# -- the sweep removes only old tmp debris -----------------------------------------------------

def test_sweep_deletes_tmp_older_than_cutoff():
    """A 0-byte "*.json.tmp" older than the cutoff is exactly the observed debris."""
    p = _write("deadbeef.json.tmp", "", age_s=FI._STALE_TMP_AGE_S + 60)
    removed = FI._sweep_stale_tmp("pending")
    assert removed == 1
    assert not os.path.exists(p)


def test_sweep_spares_young_tmp():
    """A tmp younger than the cutoff may be a live submission mid os.replace -- never touch it."""
    p = _write("fresh.json.tmp", "", age_s=1)
    removed = FI._sweep_stale_tmp("pending")
    assert removed == 0
    assert os.path.exists(p)


def test_sweep_never_touches_real_json_tasks():
    """Real "*.json" task files must survive the sweep even when old."""
    p = _write("realjob.json", '{"x": 1}', age_s=FI._STALE_TMP_AGE_S + 3600)
    removed = FI._sweep_stale_tmp("pending")
    assert removed == 0
    assert os.path.exists(p)


def test_sweep_is_quiet_on_missing_dir(monkeypatch):
    """A missing tasks dir must not raise -- the sweep is best-effort beside a live queue."""
    monkeypatch.setattr(TR, "TASKS", os.path.join(TR.TASKS, "does-not-exist"))
    assert FI._sweep_stale_tmp("pending") == 0


# -- fleet_queue counts real task files, not tmp debris ----------------------------------------

def test_fleet_queue_does_not_count_tmp_debris():
    """The defect: six 0-byte "*.json.tmp" showed as "pending 6". Young tmp must not be counted
    either -- it is not a claimable task regardless of age."""
    _write("a.json.tmp", "", age_s=1)        # young tmp, not yet swept, still not a task
    _write("b.json.tmp", "", age_s=5)
    out = FI.fleet_queue()
    line = [ln for ln in out.splitlines() if ln.startswith("pending")][0]
    assert line.split()[1] == "0", out


def test_fleet_queue_counts_real_pending_json():
    """A real queued job still counts, so the fix does not hide genuine backlog."""
    _write("real.json", '{"payload": {}}')
    out = FI.fleet_queue()
    line = [ln for ln in out.splitlines() if ln.startswith("pending")][0]
    assert line.split()[1] == "1", out


def test_fleet_queue_sweeps_old_tmp_then_counts_zero():
    """End to end: old debris present -> fleet_queue sweeps it and reports pending 0."""
    old = _write("old.json.tmp", "", age_s=FI._STALE_TMP_AGE_S + 120)
    out = FI.fleet_queue()
    line = [ln for ln in out.splitlines() if ln.startswith("pending")][0]
    assert line.split()[1] == "0", out
    assert not os.path.exists(old), "old tmp should have been swept by fleet_queue"


def test_fleet_submit_reaps_old_tmp_before_queueing():
    """Submitting a goal reaps pre-existing old debris while queueing the new job as a real json."""
    old = _write("stale.json.tmp", "", age_s=FI._STALE_TMP_AGE_S + 120)
    out = FI.fleet_submit("先月のメールを一覧して")
    assert "queued" in out
    assert not os.path.exists(old), "old tmp should have been swept by fleet_submit"
    reals = [f for f in os.listdir(_pending_dir()) if f.endswith(".json")]
    assert len(reals) == 1
