"""The swe/work clone sweep must never cost a fleet start anything.

Measured on a synthetic ~100k-file/60k-dir tree of the same shape as the real swe/work: a
discovery-only walk is ~52 s and a walk that also deletes is ~340 s -- both far past a
per-fleet-start budget, and apply() runs at the start of EVERY fleet start. So
workspace_clones() (the function apply() calls) never does the walk itself: it only decides
whether a sweep is due and, if so, hands off to a launcher that runs the real work
(_clone_sweep_run_once()) in a DETACHED PROCESS, off the critical path entirely. workspace_clones
itself must return in well under the ~0.5 s ceiling regardless of how large swe/work is or how
overdue the sweep is.

Because the real work is detached, "removes a whole clone" and "workspace_clones() is fast and
does not block" are two different claims and are tested separately:

  * Correctness of WHAT gets removed (age, in-use, junctions, interrupted deletes, the lock) is
    tested directly against _clone_sweep_run_once() -- the synchronous core the launcher runs.
  * workspace_clones()'s own job -- deciding fast, launching, never scanning inline -- is tested
    through workspace_clones() itself, with an injected launcher (a no-op spy for the
    "must not block" tests, or a real background thread for "the sweep actually completes").
    A THREAD launcher, not a subprocess, is used in tests so nothing here spawns a real process
    or invokes PowerShell; the default production launcher (a detached subprocess) is exercised
    manually via `python -m relay.fleet_retention --clone-sweep-worker`, not by these tests.

Idioms mirrored from test_a_fleet_start_does_not_walk_a_workspace.py: `_touch`, the
`_record_scandir` spy, and the junction test skipped off Windows.
"""
import io
import os
import shutil
import sys
import threading
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


def _thread_launcher(handles, in_use=_not_in_use):
    """A launcher for tests: runs the real worker in a background thread instead of a
    subprocess, so a test can `.join()` it deterministically without spawning anything real."""
    def launcher(fleet_dir, keep_days, launch_now):
        t = threading.Thread(target=R._clone_sweep_run_once,
                             args=(fleet_dir, keep_days, in_use, launch_now))
        t.start()
        handles.append(t)
    return launcher


# ---------------------------------------------------------------------------
# Correctness of the synchronous worker, _clone_sweep_run_once().
# ---------------------------------------------------------------------------

def test_an_old_unused_clone_is_removed_whole(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "django__django-main")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)
    _touch(os.path.join(clone, ".git", "objects", "pack", "a.pack"), age_days=90, now=now)

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

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
    os.utime(clone, (now - 90 * 86400.0, now - 90 * 86400.0))

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

    assert removed == []
    assert os.path.exists(deep_recent)
    assert os.path.exists(clone)


def test_an_in_use_clone_is_kept_regardless_of_age(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "old_but_running")
    _touch(os.path.join(clone, "setup.py"), age_days=999, now=now)

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _always_in_use, now)

    assert removed == []
    assert os.path.exists(clone)


def test_an_unanswerable_in_use_check_fails_closed(tmp_path):
    """When the underlying "is this running" check cannot tell (the real implementation
    returns None on a PowerShell/WMI failure), the clone is kept -- treated as in use, not as
    free to delete."""
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "cannot_tell")
    _touch(os.path.join(clone, "setup.py"), age_days=999, now=now)

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _cannot_tell, now)

    assert removed == []
    assert os.path.exists(clone)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_a_junction_directly_under_work_is_never_entered_or_removed(tmp_path):
    import _winapi

    now = time.time()
    outside = tmp_path / "not_a_clone"
    victim = _touch(str(outside / "precious.txt"), age_days=400, now=now)
    work = tmp_path / "swe" / "work"
    os.makedirs(str(work))
    _winapi.CreateJunction(str(outside), str(work / "link"))

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

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

    R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

    assert os.path.exists(victim), "a junction nested inside a clone was followed out of it"


def test_perf_sanity_hundreds_of_files_no_extra_stat_per_file(tmp_path, monkeypatch):
    """Lighter-weight CI-suitable version of the shape/scale test: a few thousand files across
    several clones, asserting the walk costs O(directories) not O(files) -- via CALL COUNTS,
    not wall-clock. A seconds-based bound is exactly the kind of assertion that goes red on a
    loaded or shared CI runner for reasons that have nothing to do with a regression here (this
    one already flaked once on this machine under unrelated background load); a count of
    os.scandir()/os.stat() calls does not move with how busy the box is, only with how much
    work the code under test actually did. The wall-clock line kept below is a generous
    backstop against a genuine algorithmic regression (an accidental O(files) walk), not the
    thing this test is really checking.
    """
    now = time.time()
    n_clones = 5
    n_pkgs = 7
    n_files_per_clone = 400
    for c in range(n_clones):
        clone = str(tmp_path / "swe" / "work" / ("clone_%d" % c))
        for i in range(n_files_per_clone):
            _touch(os.path.join(clone, "pkg%d" % (i % n_pkgs), "mod_%03d.py" % i),
                  age_days=90, now=now)

    stat_calls = []
    getmtime_calls = []
    real_stat = os.stat
    real_getmtime = os.path.getmtime

    def spy_stat(*a, **k):
        stat_calls.append(a[0] if a else None)
        return real_stat(*a, **k)

    def spy_getmtime(*a, **k):
        getmtime_calls.append(a[0] if a else None)
        return real_getmtime(*a, **k)

    monkeypatch.setattr(os, "stat", spy_stat)
    monkeypatch.setattr(os.path, "getmtime", spy_getmtime)
    scandir_calls = _record_scandir(monkeypatch)

    # DRY RUN. This test's job is to bound the DISCOVERY walk -- _newest_mtime_and_size() and
    # the scandir of swe/work itself -- which is the part this module's own code controls and
    # the part f7571a1 was about. A real (non-dry) run additionally calls shutil.rmtree() on
    # each removed clone, and rmtree does its OWN internal directory listing to delete a tree
    # (confirmed by running this same scenario non-dry: the scandir count exactly doubles, one
    # set from the walk here and one from rmtree walking the same directory again to remove
    # it) -- a stdlib implementation detail this test has no business pinning down, and pinning
    # it would make the test fail on a Python version whose rmtree walks differently despite
    # this module's own code being unchanged. dry_run=True exercises the same discovery walk
    # every real run also does, without shutil.rmtree in the picture.
    t0 = time.time()
    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now, dry_run=True)
    elapsed = time.time() - t0

    assert len(removed) == n_clones

    # os.scandir() is called exactly once per DIRECTORY entered: once for swe/work itself (the
    # leftover-.deleting- pass), once again for swe/work to list the clones, then once per
    # clone directory and once per "pkgN" directory inside it -- (1 + n_pkgs) per clone. 2000
    # files never enter this count at all; only the 1 + n_clones * (1 + n_pkgs) directories do.
    expected_scandir_calls = 2 + n_clones * (1 + n_pkgs)
    assert len(scandir_calls) == expected_scandir_calls, (
        "%d scandir() calls, expected exactly %d (proportional to %d directories, not %d "
        "files) -- got more than that means something is walking files, not directories" %
        (len(scandir_calls), expected_scandir_calls, expected_scandir_calls,
         n_clones * n_files_per_clone))

    # DirEntry.stat(follow_symlinks=False), read from the scandir() listing itself, accounts
    # for every mtime/size check; os.stat() and os.path.getmtime() are the PER-FILE (and
    # per-directory) syscalls that cost the walk f7571a1 removed, and neither belongs anywhere
    # in the walk over swe/work's contents. The one exception is os.path.isdir(work_root) at
    # the very top -- checking swe/work itself exists, once, via CPython's os.path.isdir()
    # (which calls os.stat() under the hood) -- so up to that ONE call is allowed; a second
    # would mean something started stat()ing individual entries again.
    assert len(stat_calls) <= 1, "%d bare os.stat() calls, expected at most 1: %r" % (
        len(stat_calls), stat_calls)
    assert getmtime_calls == [], (
        "%d os.path.getmtime() calls, expected zero" % len(getmtime_calls))

    # Generous backstop against an algorithmic regression, not the property under test above.
    assert elapsed < 30.0, "walk over %d files took %.2fs" % (
        n_clones * n_files_per_clone, elapsed)


# ---------------------------------------------------------------------------
# Interrupted deletes: rename-before-rmtree, and the next sweep finishing the leftover.
# ---------------------------------------------------------------------------

def test_a_leftover_deleting_directory_is_finished_by_the_next_sweep(tmp_path):
    now = time.time()
    work_root = str(tmp_path / "swe" / "work")
    leftover = os.path.join(work_root, ".deleting-half_gone-123456")
    _touch(os.path.join(leftover, "remains.txt"), age_days=1, now=now)
    # A live, recent, in-use clone beside it must be left alone -- proves the leftover cleanup
    # is a targeted finish, not a second sweep pass over everything.
    live_clone = str(tmp_path / "swe" / "work" / "live_clone")
    _touch(os.path.join(live_clone, "setup.py"), age_days=1, now=now)

    freed, removed = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

    assert not os.path.exists(leftover)
    assert os.path.exists(live_clone)
    assert ".deleting-half_gone-123456" in removed


def test_a_delete_interrupted_between_rename_and_rmtree_is_finished_next_time(
        tmp_path, monkeypatch):
    """Simulates the real failure mode: the rename succeeds (the clone's original name is
    already gone -- nothing can mistake it for live any more) but rmtree itself is interrupted.
    The leftover must survive as a `.deleting-*` name, never as the clone's own name, and the
    NEXT sweep must finish it without needing to re-decide age or in-use."""
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "clone_a")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky_rmtree(path, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated interruption")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(R.shutil, "rmtree", flaky_rmtree)

    freed1, removed1 = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)
    work_root = str(tmp_path / "swe" / "work")
    assert not os.path.exists(clone), "the original name must be gone even if rmtree failed"
    leftovers = [n for n in os.listdir(work_root) if n.startswith(".deleting-")]
    assert len(leftovers) == 1, "the interrupted delete must survive as a .deleting- name"

    freed2, removed2 = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now + 1)

    assert os.listdir(work_root) == [], "the next sweep must finish the leftover"


# ---------------------------------------------------------------------------
# The lock: two concurrent triggers must run only one sweep.
# ---------------------------------------------------------------------------

def test_two_concurrent_triggers_run_one_sweep(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "clone_a")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    entered_in_use = threading.Event()
    release_first = threading.Event()

    def slow_not_in_use(path):
        entered_in_use.set()
        release_first.wait(timeout=5)
        return False

    results = []

    def run_first():
        results.append(R._clone_sweep_run_once(str(tmp_path), 14, slow_not_in_use, now))

    t1 = threading.Thread(target=run_first)
    t1.start()
    assert entered_in_use.wait(timeout=5), "first sweep never reached its in-use check"

    # The first call already holds the lock (acquired before the in-use check runs) --
    # a second call attempted right now must bail out immediately, having done nothing.
    freed2, removed2 = R._clone_sweep_run_once(str(tmp_path), 14, _not_in_use, now)

    release_first.set()
    t1.join(timeout=5)

    assert (freed2, removed2) == (0, []), "a concurrent second sweep must not also run"
    assert results[0][1] == ["clone_a"], "the first sweep should have done the work alone"


# ---------------------------------------------------------------------------
# workspace_clones() itself: fast, off the critical path, throttled.
# ---------------------------------------------------------------------------

def test_dry_run_reports_synchronously_without_touching_the_filesystem(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "would_be_removed")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=True, keep_days=14,
                                        in_use=_not_in_use)

    assert removed == ["would_be_removed"]
    assert freed > 0
    assert os.path.exists(clone), "dry_run must not delete anything"


def test_workspace_clones_returns_fast_even_when_a_sweep_is_due(tmp_path, monkeypatch):
    now = time.time()
    for c in range(5):
        clone = str(tmp_path / "swe" / "work" / ("clone_%d" % c))
        for i in range(200):
            _touch(os.path.join(clone, "pkg", "mod_%03d.py" % i), age_days=90, now=now)

    launched = []
    seen = _record_scandir(monkeypatch)

    t0 = time.time()
    freed, removed = R.workspace_clones(
        str(tmp_path), now=now, dry_run=False, keep_days=14,
        launcher=lambda fleet_dir, keep_days, launch_now: launched.append(
            (fleet_dir, keep_days, launch_now)))
    elapsed = time.time() - t0

    assert (freed, removed) == (0, []), "the result is not known synchronously"
    assert elapsed < 0.5, "workspace_clones() took %.3fs to hand off" % elapsed
    assert len(launched) == 1
    assert launched[0][0] == str(tmp_path)
    work = os.path.normcase(os.path.abspath(str(tmp_path / "swe" / "work")))
    walked_into = [p for p in seen if p == work or p.startswith(work + os.sep)]
    assert walked_into == [], "workspace_clones() itself scanned swe/work: %s" % walked_into


def test_the_background_sweep_completes_and_removes_old_clones(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "old_clone")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    handles = []
    freed, removed = R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14,
                                        launcher=_thread_launcher(handles))

    assert (freed, removed) == (0, [])
    assert len(handles) == 1
    handles[0].join(timeout=10)

    assert not os.path.exists(clone), "the background sweep never removed the clone"


def test_throttled_second_call_does_not_launch_or_scan(tmp_path, monkeypatch):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "some_clone")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    handles = []
    R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14, sweep_hours=24,
                       launcher=_thread_launcher(handles))
    handles[0].join(timeout=10)
    assert not os.path.exists(clone), "sanity: the first sweep should have removed it"

    launched = []
    seen = _record_scandir(monkeypatch)

    freed, removed = R.workspace_clones(
        str(tmp_path), now=now + 3600, dry_run=False, keep_days=14, sweep_hours=24,
        launcher=lambda *a: launched.append(a))

    assert launched == [], "a throttled call must not launch a sweep at all"
    assert (freed, removed) == (0, [])
    work = os.path.normcase(os.path.abspath(str(tmp_path / "swe" / "work")))
    walked_into = [p for p in seen if p == work or p.startswith(work + os.sep)]
    assert walked_into == [], "the throttled call touched swe/work: %s" % walked_into


def test_a_call_after_the_throttle_window_launches_again(tmp_path):
    now = time.time()
    clone = str(tmp_path / "swe" / "work" / "some_clone")
    _touch(os.path.join(clone, "setup.py"), age_days=90, now=now)

    handles = []
    R.workspace_clones(str(tmp_path), now=now, dry_run=False, keep_days=14, sweep_hours=1,
                       launcher=_thread_launcher(handles))
    handles[0].join(timeout=10)

    clone2 = str(tmp_path / "swe" / "work" / "another_clone")
    _touch(os.path.join(clone2, "setup.py"), age_days=90, now=now)

    launched = []

    def spy_launcher(fleet_dir, keep_days, launch_now):
        launched.append(True)
        t = threading.Thread(target=R._clone_sweep_run_once,
                             args=(fleet_dir, keep_days, _not_in_use, launch_now))
        t.start()
        handles.append(t)

    R.workspace_clones(str(tmp_path), now=now + 2 * 3600, dry_run=False, keep_days=14,
                       sweep_hours=1, launcher=spy_launcher)

    assert launched == [True]
    handles[-1].join(timeout=10)
    assert not os.path.exists(clone2)
