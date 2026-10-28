# -*- coding: utf-8 -*-
"""A fleet worker's children run below the operator's windows, by one helper.

WHAT BROKE. 2026-10-27 the machine's 12 cores hit 100% (python children, ~3 cores of it) and
the Fleet Cockpit went sluggish. A fleet worker's children -- tests, analysis, builds,
background jobs -- have no reason to run at the same priority as the two windows the operator
watches (Fleet Cockpit, CopilotChat), and when they do they starve them.

WHAT THESE TESTS PIN. tools/childproc.tree_popen_kwargs is the single place every killable-tree
child is spawned through, and it now lowers the child to child_process_priority (default
below_normal) unless the caller passes priority=False. The mechanism is platform-split and that
split is the thing most likely to rot, so it is asserted directly:

  * Windows -> the priority rides in creationflags as a PRIORITY_CLASS; 'normal' adds no flag.
  * POSIX   -> a preexec_fn renices the child; 'normal' adds no preexec_fn.

The GUI windows are launched by ui/rebuild_ui.ps1 through explorer and never pass through this
helper, so they keep the desktop's ordinary priority -- this only lowers the fleet's children.
The setting is read fresh per child (MCP_CHILD_PROCESS_PRIORITY beats the file) and an
unreadable or unknown value is the default, so a child is never spawned at a WRONG priority
because the setting could not be read.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import childproc as C  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_priority_env(monkeypatch):
    """No env override unless a test sets one, so the file/default path is what is measured."""
    monkeypatch.delenv(C._PRIORITY_ENV, raising=False)
    yield


# --------------------------------------------------------------------- the configured name
def test_the_default_priority_is_below_normal(monkeypatch):
    # No env, and settings_file resolving to nothing readable -> the helper's own default.
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: "")
    assert C._priority_name() == "below_normal"
    assert C._DEFAULT_PRIORITY == "below_normal"


def test_the_env_override_beats_the_file(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "idle")
    assert C._priority_name() == "idle"


def test_an_unknown_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "turbo")   # not a real class
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: "")
    assert C._priority_name() == "below_normal"


def test_the_setting_is_read_from_the_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.txt"
    path.write_text("child_process_priority=idle\n", encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(path))
    assert C._priority_name() == "idle"


def test_an_unknown_file_value_falls_back_to_the_default(tmp_path, monkeypatch):
    path = tmp_path / "settings.txt"
    path.write_text("child_process_priority=turbo\n", encoding="utf-8")
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(path))
    assert C._priority_name() == "below_normal"


# --------------------------------------------------------------------- Windows: creationflags
@pytest.mark.skipif(os.name != "nt", reason="PRIORITY_CLASS creationflags are Windows-only")
def test_windows_below_normal_rides_in_creationflags(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    flags = C.priority_creationflags()
    assert flags == subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert flags != 0


@pytest.mark.skipif(os.name != "nt", reason="PRIORITY_CLASS creationflags are Windows-only")
def test_windows_normal_adds_no_priority_flag(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "normal")
    assert C.priority_creationflags() == 0


@pytest.mark.skipif(os.name != "nt", reason="PRIORITY_CLASS creationflags are Windows-only")
def test_windows_tree_popen_kwargs_carries_the_priority_by_default(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    kw = C.tree_popen_kwargs()
    assert kw["creationflags"] & subprocess.BELOW_NORMAL_PRIORITY_CLASS


@pytest.mark.skipif(os.name != "nt", reason="PRIORITY_CLASS creationflags are Windows-only")
def test_windows_priority_false_leaves_creationflags_without_a_class(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    kw = C.tree_popen_kwargs(priority=False)
    assert not (kw["creationflags"] & subprocess.BELOW_NORMAL_PRIORITY_CLASS)


# --------------------------------------------------------------------- POSIX: preexec_fn renice
@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_priority_creationflags_is_zero(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    assert C.priority_creationflags() == 0


@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_below_normal_gives_a_renicing_preexec(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    pre = C._priority_preexec()
    assert callable(pre)


@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_normal_adds_no_preexec(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "normal")
    assert C._priority_preexec() is None


@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_tree_popen_kwargs_adds_a_preexec_by_default(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    kw = C.tree_popen_kwargs()
    assert "preexec_fn" in kw and callable(kw["preexec_fn"])
    assert kw.get("start_new_session") is True


@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_priority_false_omits_the_preexec(monkeypatch):
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")
    kw = C.tree_popen_kwargs(priority=False)
    assert "preexec_fn" not in kw


@pytest.mark.skipif(os.name == "nt", reason="renice via preexec_fn is POSIX-only")
def test_posix_renice_preexec_swallows_its_own_errors(monkeypatch):
    # A failed renice must not stop the child from launching.
    monkeypatch.setenv(C._PRIORITY_ENV, "below_normal")

    def _boom(_inc):
        raise OSError("nice denied")

    monkeypatch.setattr(os, "nice", _boom, raising=False)
    pre = C._priority_preexec()
    assert callable(pre)
    pre()   # must not raise
