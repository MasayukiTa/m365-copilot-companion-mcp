"""The retention that runs before every fleet start must not cost the start a minute.

Measured 2026-09-24 on a cold start: 60 s between "fleet_runner started" and the run's
`started` stamp, all of it inside fleet_retention.stores(), which stat()ed each of the 101,170
files under .fleet/swe/work -- benchmark clones, worktrees and pip targets -- and deleted none.
A per-file age rule has no business in there anyway: a pack file untouched for thirty days is
still the repository.

And the ledger cap read the whole 64 MB tail of tool_events.jsonl into memory, at every start,
to free 0.1 MB.

Everything here runs against a temporary directory, never the live .fleet.
"""
import io
import json
import os
import sys
import time
import tracemalloc

import pytest

from relay import fleet_retention as R


def _touch(path, size=64, age_days=0.0, now=None):
    now = time.time() if now is None else now
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as fh:
        fh.write(b"x" * size)
    t = now - age_days * 86400.0
    os.utime(path, (t, t))
    return path


def _record_scandir(monkeypatch):
    seen = []
    real = os.scandir

    def spy(path="."):
        seen.append(os.path.normcase(os.path.abspath(os.fspath(path))))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return seen


def test_the_benchmark_workspace_is_never_entered(tmp_path, monkeypatch):
    now = time.time()
    fleet = tmp_path
    clone_file = _touch(str(fleet / "swe" / "work" / "django__django-main" / "setup.py"),
                        age_days=90, now=now)
    pip_file = _touch(str(fleet / "swe" / "work" / "_np123" / "numpy" / "core.py"),
                      age_days=90, now=now)
    old_attempt = _touch(str(fleet / "swe" / "attempts" / "a1.json"), age_days=90, now=now)
    seen = _record_scandir(monkeypatch)

    freed, removed = R.stores(str(fleet), now=now, keep_days=30)

    work = os.path.normcase(os.path.abspath(str(fleet / "swe" / "work")))
    walked_into = [p for p in seen if p == work or p.startswith(work + os.sep)]
    assert walked_into == [], "the store rule listed the workspace: %s" % walked_into
    assert os.path.exists(clone_file) and os.path.exists(pip_file)
    assert not os.path.exists(old_attempt), "the per-run output beside it stopped ageing out"
    assert removed == [os.path.join("swe", "attempts", "a1.json")]


def test_a_checkout_anywhere_in_a_store_is_one_unit(tmp_path):
    now = time.time()
    os.makedirs(str(tmp_path / "transcripts" / "scratch_repo" / ".git"))
    inside = _touch(str(tmp_path / "transcripts" / "scratch_repo" / "src" / "a.py"),
                    age_days=90, now=now)
    beside = _touch(str(tmp_path / "transcripts" / "r1_a0_w0.jsonl"), age_days=90, now=now)
    R.stores(str(tmp_path), now=now, keep_days=30)
    assert os.path.exists(inside), "a file was deleted out of the middle of a checkout"
    assert not os.path.exists(beside)


def test_young_files_cost_no_stat_call_each(tmp_path, monkeypatch):
    """The listing already carries the times; a stat per file is what made the walk a minute."""
    now = time.time()
    for i in range(300):
        _touch(str(tmp_path / "transcripts" / ("r%03d_a0_w0.jsonl" % i)), age_days=1, now=now)
    calls = []
    real = os.stat

    def spy(*a, **k):
        calls.append(a[0] if a else None)
        return real(*a, **k)

    monkeypatch.setattr(os, "stat", spy)
    freed, removed = R.stores(str(tmp_path), now=now, keep_days=30)
    assert removed == []
    assert len(calls) < 10, "%d stat calls for 300 young files" % len(calls)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_a_junction_is_not_followed_out_of_the_store(tmp_path):
    import _winapi

    now = time.time()
    outside = tmp_path / "not_a_store"
    victim = _touch(str(outside / "precious.txt"), age_days=400, now=now)
    os.makedirs(str(tmp_path / "fleet" / "transcripts"))
    _winapi.CreateJunction(str(outside), str(tmp_path / "fleet" / "transcripts" / "link"))
    R.stores(str(tmp_path / "fleet"), now=now, keep_days=30)
    assert os.path.exists(victim), "the age rule followed a junction and deleted outside .fleet"


def _ledger(path, lines, pad=100):
    with io.open(path, "w", encoding="utf-8") as fh:
        for i in range(lines):
            fh.write(json.dumps({"i": i, "pad": "y" * pad}) + "\n")


def test_capping_a_ledger_does_not_hold_its_tail_in_memory(tmp_path):
    p = str(tmp_path / "tool_events.jsonl")
    _ledger(p, 90000)                                    # ~10.6 MB
    tracemalloc.start()
    try:
        R.cap_jsonl(str(tmp_path), max_mb=8)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert os.path.getsize(p) <= 8 * 1024 * 1024
    # Two copy chunks can be alive at once (the next is read before the last is freed); the
    # whole-tail version held all 7-8 MB.
    assert peak < 3 * 1024 * 1024, "peak %.1f MB to cap an 8 MB tail" % (peak / 1048576.0)
    lines = io.open(p, encoding="utf-8").read().splitlines()
    assert json.loads(lines[-1])["i"] == 89999
    for ln in lines:
        json.loads(ln)


def test_a_ledger_at_the_ceiling_is_not_rewritten_at_every_start(tmp_path):
    """Cut to exactly the ceiling, the next day's writes put it back over and the next start
    rewrote all 64 MB again. Cut with headroom, a little more writing leaves it alone."""
    p = str(tmp_path / "tool_events.jsonl")
    _ledger(p, 50000)
    limit = 4 * 1024 * 1024
    R.cap_jsonl(str(tmp_path), max_mb=4)
    after_first = os.path.getsize(p)
    assert after_first <= limit
    with io.open(p, "a", encoding="utf-8") as fh:            # one more "day" of writing: 2%
        for i in range(700):
            fh.write(json.dumps({"i": 90000 + i, "pad": "y" * 100}) + "\n")
    assert os.path.getsize(p) > after_first
    freed, trimmed = R.cap_jsonl(str(tmp_path), max_mb=4)
    assert trimmed == [], "rewritten again after %d bytes of growth" % (
        os.path.getsize(p) - after_first)


def test_a_ledger_held_open_is_still_capped(tmp_path, monkeypatch):
    """os.replace is refused while another process holds the file without delete sharing;
    the tail is then streamed back over the original instead."""
    p = str(tmp_path / "activity.jsonl")
    _ledger(p, 20000)

    def refused(*a, **k):
        raise PermissionError(13, "held open")

    monkeypatch.setattr(os, "replace", refused)
    freed, trimmed = R.cap_jsonl(str(tmp_path), max_mb=0.5)
    assert trimmed == ["activity.jsonl"]
    assert os.path.getsize(p) <= 0.5 * 1024 * 1024
    lines = io.open(p, encoding="utf-8").read().splitlines()
    assert json.loads(lines[-1])["i"] == 19999
    assert not [n for n in os.listdir(str(tmp_path)) if n.endswith(".captmp")]
