"""workspace_clones() removes a whole benchmark clone under swe/work as one unit, once it is
both old (nothing anywhere inside it, recursively, has a recent mtime) and unused (no live
process's command line names its path) -- never file by file, which is what corrupts a
checkout rather than cleaning it. See fleet_retention.py's own comment ahead of
CLONE_KEEP_DAYS for the full reasoning and the STORE_SKIP precedent this follows.

Idioms mirrored from test_a_fleet_start_does_not_walk_a_workspace.py: `_touch`, the
`_record_scandir` spy, and the junction test skipped off Windows.
"""
import io
import json
import os
import sys
import time

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


def _not_in_use(path):
    return False


def _always_in_use(path):
    return True


def _cannot_tell(path):
    return None


def test_an_old_unused_clone_is_removed_whole(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "django__django-main")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)
    _touch(os.path.join(clone, ".git", "objects", "pack", "a.pack"), age_days=90, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_not_in_use)

    assert removed == ["django__django-main"]
    assert freed > 0
    assert not os.path.exists(clone), "the clone directory itself must be gone, not just emptied"


def test_a_clone_with_a_recent_file_deep_inside_is_kept(tmp_path):
    """The clone's own top-level directory entry is old, but something nested several
    directories down (a pip install into a venv) is recent -- proves the true recursive
    newest-mtime is used, not the top dir's own mtime and not a shallow scan."""
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "_np123" / "numpy")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)
    deep_recent = _touch(
        os.path.join(clone, "venv", "lib", "site-packages", "pkg", "new_dep", "mod.py"),
        age_days=1, now=now)
    # Age the clone's OWN directory entry too, so a rule that only checked the top dir's mtime
    # would wrongly call this old.
    os.utime(clone, (now - 90 * 86400.0, now - 90 * 86400.0))

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_not_in_use)

    assert removed == []
    assert os.path.exists(deep_recent)
    assert os.path.exists(clone)


def test_an_in_use_clone_is_kept_regardless_of_age(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "old_but_running")
    _touch(os.path.join(clone, "setup.py"), age_days=999, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_always_in_use)

    assert removed == []
    assert os.path.exists(clone)


def test_an_unanswerable_in_use_check_fails_closed(tmp_path):
    """When the underlying "is this running" check cannot tell (the real implementation
    returns None on a PowerShell/WMI failure), the clone is kept -- treated as in use, not as
    free to delete."""
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "cannot_tell")
    _touch(os.path.join(clone, "setup.py"), age_days=999, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_cannot_tell)

    assert removed == []
    assert os.path.exists(clone)


def test_dry_run_reports_without_touching_the_filesystem(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "would_be_removed")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=True, keep_days=14,
                                        in_use=_not_in_use)

    assert removed == ["would_be_removed"]
    assert freed > 0
    assert os.path.exists(clone), "dry_run must not delete anything"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_a_junction_directly_under_work_is_never_entered_or_removed(tmp_path):
    import _winapi

    now = time.time()
    outside = tmp_path / "not_a_clone"
    victim = _touch(str(outside / "precious.txt"), age_days=400, now=now)
    work = tmp_path / "swe" / "work"
    os.makedirs(str(work))
    _winapi.CreateJunction(str(outside), str(work / "link"))

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_not_in_use)

    assert removed == [], "a junction under swe/work must never be treated as a clone"
    assert os.path.exists(victim), "the junction target must never be entered, let alone removed"
    assert os.path.exists(str(work / "link")), "the junction itself must not be removed either"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_a_junction_nested_inside_a_clone_is_never_followed(tmp_path):
    import _winapi

    now = time.time()
    outside = tmp_path / "not_in_the_clone"
    victim = _touch(str(outside / "precious.txt"), age_days=400, now=now)
    clone = tmp_path / "swe" / "work" / "some_clone"
    _touch(str(clone / "setup.py"), age_days=90, now=now)
    _winapi.CreateJunction(str(outside), str(clone / "linked_in"))

    R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14, in_use=_not_in_use)

    assert os.path.exists(victim), "a junction nested inside a clone was followed out of it"


def test_throttled_second_call_does_not_re_walk(tmp_path, monkeypatch):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "some_clone")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    # First call: walk happens, records the sweep timestamp.
    R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14, in_use=_not_in_use,
                       sweep_hours=24)
    assert not os.path.exists(clone), "sanity: the first call should have removed it"

    # Recreate something to remove, then call again within the throttle window.
    clone2 = str(tmp_path / "swe" / "work" / "another_clone")
    _touch(os.path.join(clone2, "setup.py"), age_days=90, now=now)
    seen = _record_scandir(monkeypatch)

    freed, removed = R.workspace_clones(str(tmp_path), now=now + 3600, dry_run=False,
                                        keep_days=14, in_use=_not_in_use, sweep_hours=24)

    assert removed == [], "a throttled call must not remove anything"
    assert os.path.exists(clone2), "a throttled call must not have walked far enough to see it"
    work = os.path.normcase(os.path.abspath(str(tmp_path / "swe" / "work")))
    walked_into = [p for p in seen if p == work or p.startswith(work + os.sep)]
    assert walked_into == [], "the throttled call re-walked swe/work: %s" % walked_into


def test_a_call_after_the_throttle_window_walks_again(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "some_clone")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14, in_use=_not_in_use,
                       sweep_hours=1)

    clone2 = str(tmp_path / "swe" / "work" / "another_clone")
    _touch(os.path.join(clone2, "setup.py"), age_days=90, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now + 2 * 3600, dry_run=False,
                                        keep_days=14, in_use=_not_in_use, sweep_hours=1)

    assert removed == ["another_clone"]
    assert not os.path.exists(clone2)


def test_perf_sanity_hundreds_of_files_no_extra_stat_per_file(tmp_path, monkeypatch):
    """Lighter-weight CI-suitable version of the shape/scale test: a few thousand files across
    several clones, asserting the walk is fast and does not call os.stat/os.path.getmtime per
    file beyond what DirEntry.stat() (via scandir) already provides for free."""
    now = time.time()
    for c in range(5):
        clone = str(tmp_path / "swe" / "work" / ("clone_%d" % c))
        for i in range(400):
            _touch(os.path.join(clone, "pkg%d" % (i % 7), "mod_%03d.py" % i),
                  age_days=90, now=now)

    calls = []
    real_stat = os.stat
    real_getmtime = os.path.getmtime

    def spy_stat(*a, **k):
        calls.append(("stat", a[0] if a else None))
        return real_stat(*a, **k)

    def spy_getmtime(*a, **k):
        calls.append(("getmtime", a[0] if a else None))
        return real_getmtime(*a, **k)

    monkeypatch.setattr(os, "stat", spy_stat)
    monkeypatch.setattr(os.path, "getmtime", spy_getmtime)

    t0 = time.time()
    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        in_use=_not_in_use)
    elapsed = time.time() - t0

    assert len(removed) == 5
    assert elapsed < 5.0, "walk over 2000 files took %.2fs" % elapsed
    assert len(calls) < 20, "%d os.stat/getmtime calls for 2000 files" % len(calls)
