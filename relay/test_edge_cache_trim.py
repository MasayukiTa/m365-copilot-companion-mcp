# -*- coding: utf-8 -*-
"""Clearing the browser caches this project's own Edges accumulate, and nothing else.

WHY IT EXISTS. Disk is the binding constraint on this machine and has stopped a benchmark
mid-run. Measured during one long run: the three profiles this project drives held 2.17 GB, of
which 1.76 GB was cache. The standing rule when disk runs low is to free space rather than lower
the fleet's floor, and this is space that regenerates itself.

WHAT MAKES IT SAFE, and what these tests hold:
  - the user's OWN Edge (2.94 GB, and theirs) is unreachable from here even if a caller asks;
  - a profile with a live Edge is left alone, decided fail-closed;
  - sign-in survives, because a signed-out profile surfaces a login tab in front of the
    operator, which is the one thing the browser side of this project may not do.
"""
import os

import pytest

from relay import edge_recover as E


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Three managed profiles and one that is not ours, each with cache and with secrets."""
    for marker in ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge",
                   "User Data"):
        prof = tmp_path / marker / "Default"
        for name in ("Cache", "Code Cache", "GPUCache", "Service Worker"):
            d = prof / name
            d.mkdir(parents=True)
            (d / "blob").write_bytes(b"x" * 1000)
        (prof / "Cookies").write_bytes(b"session")
        (prof / "Login Data").write_bytes(b"token")
        (prof / "Local Storage").mkdir()
        (prof / "Local Storage" / "leveldb").write_bytes(b"state")
    monkeypatch.setattr(E, "profile_is_running", lambda m: False)
    return str(tmp_path)


def survives(base, marker):
    prof = os.path.join(base, marker, "Default")
    return (os.path.exists(os.path.join(prof, "Cookies"))
            and os.path.exists(os.path.join(prof, "Login Data"))
            and os.path.exists(os.path.join(prof, "Local Storage", "leveldb")))


# -- what it must never touch ------------------------------------------------------------

def test_the_users_own_edge_is_unreachable_even_when_named(base):
    """The only profile on this machine bigger than ours is the user's, and it is theirs. A
    caller passing its directory in must be refused, not obeyed."""
    freed, notes = E.trim_profile_caches(["User Data"], base=base)
    assert freed == 0
    assert any("refused" in n and "not a managed profile" in n for n in notes)
    assert os.path.exists(os.path.join(base, "User Data", "Default", "Cache", "blob"))


def test_the_default_sweep_does_not_include_it(base):
    E.trim_profile_caches(base=base)
    assert os.path.exists(os.path.join(base, "User Data", "Default", "Cache", "blob"))


def test_sign_in_survives_a_trim(base):
    """A signed-out profile surfaces a login tab in front of the operator. Cookies, Login Data
    and Local Storage are therefore not cache, whatever the disk situation."""
    E.trim_profile_caches(base=base)
    for marker in ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge"):
        assert survives(base, marker), marker


def test_a_running_profile_is_left_alone(base, monkeypatch):
    """Deleting files under a live browser is corruption, and it is touching a process this
    project did not start."""
    monkeypatch.setattr(E, "profile_is_running",
                        lambda m: m == "copilot-companion-edge")
    _, notes = E.trim_profile_caches(base=base)
    assert any("copilot-companion-edge: in use" in n for n in notes)
    assert os.path.exists(os.path.join(base, "copilot-companion-edge",
                                       "Default", "Cache", "blob"))
    assert not os.path.exists(os.path.join(base, "copilot-eval-edge",
                                           "Default", "Cache", "blob"))


def test_an_unreadable_process_list_stops_the_trim_rather_than_licensing_it(monkeypatch):
    """FAIL CLOSED. 'I could not tell whether it was running' must not read as 'it was not'."""
    import builtins
    real = builtins.__import__

    def no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    assert E.profile_is_running("copilot-eval-edge") is True


def test_a_directory_merely_living_under_a_cache_shaped_path_is_not_cache(tmp_path, monkeypatch):
    """Matching a substring of the whole path instead of the directory's own name would take
    the cookies with it whenever a profile sits under, say, C:/cache/."""
    root = tmp_path / "cache" / "copilot-eval-edge" / "Default"
    root.mkdir(parents=True)
    (root / "Cookies").write_bytes(b"session")
    (root / "Cache").mkdir()
    (root / "Cache" / "blob").write_bytes(b"x" * 100)
    monkeypatch.setattr(E, "profile_is_running", lambda m: False)
    E.trim_profile_caches(["copilot-eval-edge"], base=str(tmp_path / "cache"))
    assert (root / "Cookies").exists()
    assert not (root / "Cache").exists()


# -- what it does ------------------------------------------------------------------------

def test_it_frees_the_cache_and_says_how_much(base):
    freed, notes = E.trim_profile_caches(base=base)
    assert freed > 0
    assert len(notes) == 3
    for marker in ("copilot-companion-edge", "copilot-bridge-edge", "copilot-eval-edge"):
        assert not os.path.exists(os.path.join(base, marker, "Default", "Cache", "blob"))
        assert not os.path.exists(os.path.join(base, marker, "Default", "Service Worker"))


def test_a_dry_run_reports_without_deleting(base):
    """Disk decisions on this machine have been made on guesses before. Being able to ask what
    a trim would free, without taking the risk, is the difference."""
    freed, notes = E.trim_profile_caches(base=base, dry_run=True)
    assert freed > 0
    assert any("would free" in n for n in notes)
    assert os.path.exists(os.path.join(base, "copilot-eval-edge", "Default", "Cache", "blob"))


def test_an_absent_profile_is_reported_not_crashed(base):
    import shutil as sh
    sh.rmtree(os.path.join(base, "copilot-eval-edge"))
    freed, notes = E.trim_profile_caches(base=base)
    assert any("copilot-eval-edge: absent" in n for n in notes)
    assert freed > 0


def test_every_managed_profile_is_swept_by_default(base):
    """The same omission -- adding a profile and not sweeping the places that enumerate them --
    has happened four times in this file's history. The default list is built from the map."""
    _, notes = E.trim_profile_caches(base=base)
    named = " ".join(notes)
    for marker in E.MANAGED_EDGE_PROFILES.values():
        assert marker in named, marker
