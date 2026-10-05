# -*- coding: utf-8 -*-
"""relay/code_staleness.py: the shared half of "is this process running the code on disk?".

The supervisor half (PowerShell) is pinned by scripts/test_supervisor_stale_code.py; here the
Python module, the setting reader, and the bridge's additive /status field."""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import code_staleness as cs  # noqa: E402


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.txt"
    from tools import settings_path
    monkeypatch.setattr(settings_path, "settings_file", lambda: str(path))
    return path


def test_the_setting_defaults_to_on_and_reads_off(settings):
    assert cs.self_restart_setting() == "on"                       # no file
    settings.write_text("supervisor_self_restart=off\n", encoding="utf-8")
    assert cs.self_restart_setting() == "off"
    settings.write_text("supervisor_self_restart= OFF \n", encoding="utf-8")
    assert cs.self_restart_setting() == "off"
    settings.write_text("supervisor_self_restart=maybe\n", encoding="utf-8")
    assert cs.self_restart_setting() == "on"                       # junk -> default


def test_the_registry_agrees_with_the_reader():
    from tools import settings_keys
    key = settings_keys.KEYS["supervisor_self_restart"] if hasattr(settings_keys, "KEYS") else None
    if key is None:
        pytest.skip("registry layout changed; the registry test covers the declaration")
    assert key.default == cs.SELF_RESTART_DEFAULT == "on"


def test_unchanged_files_are_not_hashed_again(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two", encoding="utf-8")
    w = cs.CodeWatch(str(tmp_path), ("a.txt", "b.txt"))
    calls = []
    real = cs.file_sha256
    monkeypatch.setattr(cs, "file_sha256", lambda p: (calls.append(p), real(p))[1])
    for _ in range(5):
        assert w.changed() == [] and not w.stale()
    assert calls == [], "an unchanged stat must not cost a hash"


def test_a_changed_file_is_named_and_a_restored_one_is_current_again(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one", encoding="utf-8")
    w = cs.CodeWatch(str(tmp_path), ("a.txt",))
    a.write_text("one plus", encoding="utf-8")
    assert w.changed() == ["a.txt"] and w.stale()
    a.write_text("one", encoding="utf-8")
    os.utime(a, (1, 1))                                            # restored text, different mtime
    assert w.changed() == [], "identical content is not stale however the mtime moved"


def test_a_missing_file_is_a_change_and_never_raises(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one", encoding="utf-8")
    w = cs.CodeWatch(str(tmp_path), ("a.txt", "gone.txt"))
    assert w.changed() == []                                       # gone from the start: no change
    a.unlink()
    assert w.changed() == ["a.txt"]


def test_the_real_file_lists_exist():
    for rel in cs.SUPERVISOR_CODE_FILES + cs.BRIDGE_CODE_FILES:
        assert os.path.isfile(os.path.join(REPO, rel)), rel


def test_the_bridge_status_route_reports_code_stale_without_the_token_naming_files():
    src = open(os.path.join(REPO, "bridge", "copilot_bridge.py"), encoding="utf-8").read()
    assert "_CODE_WATCH = CodeWatch(str(REPO), BRIDGE_CODE_FILES)" in src
    assert '"code_stale": bool(_code_changed)' in src
    # file names are for the token holder only, like conversation / active_sid
    assert 'status.pop("code_changed", None)' in src
