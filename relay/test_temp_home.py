"""The temp home (%TEMP%\\m365-companion\\) and the age sweep that covers it.

Every test runs against a pytest directory standing in for %TEMP% -- the sweep deletes files, so
a test that pointed it at the real temp directory would be the failure it exists to prevent.
(conftest.py also redirects relay.temp_home.system_temp() for every test.)
"""
import os
import subprocess
import sys
import time

import pytest

from relay import fleet_retention as R
from relay import temp_home as TH

HOUR = 3600.0


def _file(path, age_h=0.0, size=32, now=None):
    now = time.time() if now is None else now
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    t = now - age_h * HOUR
    os.utime(path, (t, t))
    return path


def _age_dir(path, age_h, now):
    t = now - age_h * HOUR
    os.utime(path, (t, t))


def _mklink_dir(link, target):
    """A directory link without admin: symlink if allowed, else a junction. None if neither."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True)
        return r.returncode == 0 and os.path.exists(link)
    return False


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "systemp"
    r.mkdir()
    return r


# -- the helper ------------------------------------------------------------------------------

def test_temp_home_is_created_lazily_under_the_system_temp(monkeypatch, root):
    monkeypatch.setattr(TH, "system_temp", lambda: str(root))
    assert not (root / TH.HOME_NAME).exists()
    assert TH.home_path() == str(root / TH.HOME_NAME)
    assert not (root / TH.HOME_NAME).exists(), "home_path() must not create anything"
    got = TH.temp_home()
    assert got == str(root / TH.HOME_NAME) and os.path.isdir(got)


def test_temp_dir_makes_named_subdirs_and_cannot_leave_the_home(monkeypatch, root):
    monkeypatch.setattr(TH, "system_temp", lambda: str(root))
    home = TH.temp_home()
    nested = TH.temp_dir("agents/fix-12")
    assert nested == os.path.join(home, "agents", "fix-12") and os.path.isdir(nested)
    for evil in ("../../escape", "..\\..\\escape", "/abs/path", "C:\\x\\y", "a/../../b"):
        got = TH.temp_dir(evil)
        assert os.path.commonpath([os.path.realpath(got), os.path.realpath(home)]) \
            == os.path.realpath(home), evil
    assert TH.temp_dir("") == home


def test_temp_home_falls_back_to_the_plain_temp_when_it_cannot_be_made(monkeypatch, root):
    monkeypatch.setattr(TH, "system_temp", lambda: str(root))
    (root / TH.HOME_NAME).write_text("a file where the directory should be")
    assert TH.temp_home() == str(root)


def test_job_logs_are_written_under_the_home(monkeypatch, root):
    from tools import jobs
    monkeypatch.setattr(TH, "system_temp", lambda: str(root))
    out, err = jobs._create_log_paths()
    home = str(root / TH.HOME_NAME)
    assert out.startswith(home) and err.startswith(home)
    assert out.endswith(".out.log") and err.endswith(".err.log")


# -- the sweep: what it removes --------------------------------------------------------------

def test_only_old_entries_under_the_home_are_removed(root):
    now = time.time()
    home = root / "m365-companion"
    old = _file(str(home / "old.txt"), age_h=30, now=now)
    new = _file(str(home / "new.txt"), age_h=1, now=now)
    olddir = _file(str(home / "agents" / "a" / "x.bin"), age_h=48, now=now)
    freed, items = R.sweep_temp_home(now=now, temp_root=str(root))
    assert not os.path.exists(old)
    assert os.path.exists(new)
    assert not os.path.exists(str(home / "agents"))
    assert freed >= 64 and len(items) == 2
    assert olddir not in items  # the entry is the top-level directory, reported once


def test_a_directory_with_one_recent_file_inside_is_kept_whole(root):
    now = time.time()
    home = root / "m365-companion"
    _file(str(home / "venv" / "lib" / "old.py"), age_h=100, now=now)
    keep = _file(str(home / "venv" / "lib" / "fresh.py"), age_h=1, now=now)
    _age_dir(str(home / "venv"), 100, now)
    R.sweep_temp_home(now=now, temp_root=str(root))
    assert os.path.exists(keep)
    assert os.path.exists(str(home / "venv" / "lib" / "old.py"))


def test_siblings_and_lookalikes_outside_the_home_are_never_touched(root):
    now = time.time()
    keep = [
        _file(str(root / "someone_elses.tmp"), age_h=999, now=now),
        _file(str(root / "m365-companion-supervisor.log"), age_h=999, now=now),
        _file(str(root / "m365-companion2" / "x"), age_h=999, now=now),
        _file(str(root / "m365-copilot-companion-mcp-jobs" / "ab.out.log"), age_h=999, now=now),
        _file(str(root / "copilot_paste_zzzzzzzz.png"), age_h=999, now=now),        # not 8 hex
        _file(str(root / "copilot_paste_0123abcd.png.bak"), age_h=999, now=now),    # not .png
        _file(str(root / "my_copilot_paste_0123abcd.png"), age_h=999, now=now),     # prefix
        _file(str(root / "playwright-artifacts-keepme" / "trace.zip"), age_h=999, now=now),
        _file(str(root / "playwright-artifactsX"), age_h=999, now=now),
    ]
    freed, items = R.sweep_temp_home(now=now, temp_root=str(root))
    assert items == [] and freed == 0
    assert all(os.path.exists(p) for p in keep)


def test_pasted_images_are_swept_by_exact_name_and_age(root):
    now = time.time()
    old = _file(str(root / "copilot_paste_0123abcd.png"), age_h=30, now=now)
    new = _file(str(root / "copilot_paste_89abcdef.png"), age_h=2, now=now)
    freed, items = R.sweep_temp_home(now=now, temp_root=str(root))
    assert not os.path.exists(old) and os.path.exists(new)
    assert items == [old] and freed == 32


def test_playwright_artifacts_only_when_empty_and_old(root):
    now = time.time()
    empty_old = root / "playwright-artifacts-AbC123"
    empty_old.mkdir()
    _age_dir(str(empty_old), 7, now)
    empty_new = root / "playwright-artifacts-new1"
    empty_new.mkdir()
    _age_dir(str(empty_new), 1, now)
    full_old = root / "playwright-artifacts-full1"
    full_old.mkdir()
    _file(str(full_old / "video.webm"), age_h=900, now=now)
    _age_dir(str(full_old), 900, now)
    R.sweep_temp_home(now=now, temp_root=str(root))
    assert not empty_old.exists()
    assert empty_new.exists() and full_old.exists()
    assert (full_old / "video.webm").exists()


def test_the_age_limit_is_a_parameter(root):
    now = time.time()
    p = _file(str(root / "m365-companion" / "f"), age_h=3, now=now)
    R.sweep_temp_home(now=now, temp_root=str(root))
    assert os.path.exists(p)
    R.sweep_temp_home(now=now, temp_root=str(root), max_age_h=2)
    assert not os.path.exists(p)


def test_dry_run_reports_and_removes_nothing(root):
    now = time.time()
    a = _file(str(root / "m365-companion" / "old"), age_h=50, now=now)
    b = _file(str(root / "copilot_paste_0123abcd.png"), age_h=50, now=now)
    e = root / "playwright-artifacts-x1"
    e.mkdir()
    _age_dir(str(e), 50, now)
    freed, items = R.sweep_temp_home(now=now, dry_run=True, temp_root=str(root))
    assert len(items) == 3 and freed == 64
    assert os.path.exists(a) and os.path.exists(b) and e.exists()


def test_a_missing_home_or_temp_root_is_not_an_error(tmp_path):
    assert R.sweep_temp_home(temp_root=str(tmp_path / "nowhere")) == (0, [])
    assert R.sweep_temp_home(temp_root=str(tmp_path)) == (0, [])


# -- the sweep: how safely it removes --------------------------------------------------------

def test_a_locked_file_is_skipped_without_failing(monkeypatch, root):
    now = time.time()
    home = root / "m365-companion"
    locked = _file(str(home / "d" / "locked.dll"), age_h=60, now=now)
    other = _file(str(home / "d" / "free.txt"), age_h=60, now=now)
    plain = _file(str(home / "plain_old.txt"), age_h=60, now=now)
    real_remove = os.remove

    def fake_remove(path, *a, **k):
        if "locked" in os.path.basename(str(path)):
            raise PermissionError(32, "in use")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(R.os, "remove", fake_remove)
    freed, items = R.sweep_temp_home(now=now, temp_root=str(root))   # must not raise
    assert os.path.exists(locked), "a locked file must survive"
    assert not os.path.exists(other) and not os.path.exists(plain)
    assert os.path.isdir(os.path.dirname(locked)), "its directory cannot go while it is there"


def test_an_unexpected_error_never_reaches_the_caller(monkeypatch, root):
    def boom(*a, **k):
        raise RuntimeError("scandir exploded")
    monkeypatch.setattr(R.os, "scandir", boom)
    assert R.sweep_temp_home(temp_root=str(root)) == (0, [])


def test_a_link_inside_the_home_is_removed_as_a_link_and_never_followed(root, tmp_path):
    now = time.time()
    outside = tmp_path / "outside"
    precious = _file(str(outside / "precious.txt"), age_h=999, now=now)
    home = root / "m365-companion"
    home.mkdir()
    # a nested link inside an old directory, and a top-level one
    d = home / "old_dir"
    d.mkdir()
    _file(str(d / "f.txt"), age_h=60, now=now)
    if not (_mklink_dir(str(d / "nested_link"), str(outside))
            and _mklink_dir(str(home / "top_link"), str(outside))):
        pytest.skip("neither symlinks nor junctions can be created here")
    _age_dir(str(d), 60, now)
    R.sweep_temp_home(now=now, temp_root=str(root))
    assert os.path.exists(precious), "the sweep followed a link out of the home"
    assert not os.path.lexists(str(d / "nested_link"))


def test_a_linked_home_is_not_swept(root, tmp_path):
    now = time.time()
    real = tmp_path / "real_home"
    victim = _file(str(real / "old.txt"), age_h=999, now=now)
    if not _mklink_dir(str(root / "m365-companion"), str(real)):
        pytest.skip("neither symlinks nor junctions can be created here")
    R.sweep_temp_home(now=now, temp_root=str(root))
    assert os.path.exists(victim)


# -- wiring ----------------------------------------------------------------------------------

def test_apply_runs_the_temp_rule_and_reports_it(monkeypatch, tmp_path):
    now = time.time()
    fleet = tmp_path / ".fleet"
    fleet.mkdir()
    sys_temp = tmp_path / "systemp"
    old = _file(str(sys_temp / "m365-companion" / "agents" / "x" / "old.bin"), age_h=48,
                size=2048, now=now)
    new = _file(str(sys_temp / "m365-companion" / "fresh.txt"), age_h=1, now=now)
    monkeypatch.setattr(TH, "system_temp", lambda: str(sys_temp))
    rep = R.apply(fleet_dir=str(fleet), now=now, dry_run=False)
    assert "temp_home" in rep["rules"], rep
    r = rep["rules"]["temp_home"]
    assert "error" not in r and r["count"] == 1 and r["freed_bytes"] == 2048
    assert not os.path.exists(old) and os.path.exists(new)

    dry = R.apply(fleet_dir=str(fleet), now=now + 100 * HOUR, dry_run=True)
    assert dry["rules"]["temp_home"]["count"] == 1 and os.path.exists(new)


def test_the_real_temp_is_not_what_tests_sweep():
    """conftest redirects system_temp(); if this ever fails, a test could delete real files."""
    import tempfile
    assert os.path.realpath(TH.system_temp()) != os.path.realpath(tempfile.gettempdir())
