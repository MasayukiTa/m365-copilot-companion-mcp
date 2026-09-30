# -*- coding: utf-8 -*-
"""effort_policy setting: precedence env > settings.txt > off, conflict surfacing, mtime refresh.

Hermetic: the real environment path is redirected by patching tools.settings_path.NEW_PATH
to a tmp file (and APPDATA/HOME away so the old location cannot be found); the injected-env
path only reads the file named by env["MCP_EFFORT_POLICY_SETTINGS"].
"""
import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import effort_policy as ep          # noqa: E402
from tools import settings_path as SP          # noqa: E402


def _put(path, text):
    with io.open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    st = os.stat(str(path))
    # force a distinct mtime even on coarse filesystems
    os.utime(str(path), ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    """env=None path: settings.txt is tmp, MCP_EFFORT_POLICY unset, conflict memory clear."""
    f = tmp_path / "settings.txt"
    monkeypatch.setattr(SP, "NEW_PATH", str(f))
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    monkeypatch.delenv("MCP_EFFORT_POLICY", raising=False)
    ep._warned_conflict.clear()
    ep._settings_cache.clear()
    return f


def test_default_off_when_nothing_set(real_env):
    assert ep.mode_info() == ("off", "default", False)
    assert ep.mode() == "off"


@pytest.mark.parametrize("v", ["off", "shadow", "on"])
def test_settings_value_is_used(real_env, v):
    _put(real_env, "maxtabs=3\neffort_policy=%s\n" % v)
    assert ep.mode_info() == (v, "settings", False)


def test_settings_case_bom_and_spaces(real_env):
    real_env.write_bytes(b"\xef\xbb\xbfeffort_policy= Shadow \r\n")
    assert ep.mode_info() == ("shadow", "settings", False)


def test_env_beats_settings_and_conflict_logged_once(real_env, monkeypatch):
    _put(real_env, "effort_policy=off\n")
    monkeypatch.setenv("MCP_EFFORT_POLICY", "on")
    said = []
    for _ in range(3):
        assert ep.mode_info(log=said.append) == ("on", "env", True)
    assert said == ["[effort_policy] env MCP_EFFORT_POLICY=on overrides settings.txt "
                    "effort_policy=off"]


def test_env_equal_to_settings_is_not_a_conflict(real_env, monkeypatch):
    _put(real_env, "effort_policy=shadow\n")
    monkeypatch.setenv("MCP_EFFORT_POLICY", "shadow")
    said = []
    assert ep.mode_info(log=said.append) == ("shadow", "env", False)
    assert said == []


def test_env_without_settings_key_is_not_a_conflict(real_env, monkeypatch):
    _put(real_env, "maxtabs=3\n")
    monkeypatch.setenv("MCP_EFFORT_POLICY", "on")
    assert ep.mode_info() == ("on", "env", False)


def test_invalid_env_falls_through_to_settings(real_env, monkeypatch):
    _put(real_env, "effort_policy=shadow\n")
    monkeypatch.setenv("MCP_EFFORT_POLICY", "turbo")
    assert ep.mode_info() == ("shadow", "settings", False)


def test_invalid_settings_value_is_default_off(real_env):
    _put(real_env, "effort_policy=turbo\n")
    assert ep.mode_info() == ("off", "default", False)


def test_missing_file_is_default_off(real_env):
    assert not os.path.exists(str(real_env))
    assert ep.mode_info() == ("off", "default", False)


def test_change_in_gui_is_seen_by_next_call_without_restart(real_env):
    """Granularity: every mode() call re-stats the file, so the NEXT worker / fan-out / turn
    evaluation in a running process sees it; a decision already taken is not revisited."""
    _put(real_env, "effort_policy=off\n")
    assert ep.mode() == "off"
    _put(real_env, "effort_policy=on\n")
    assert ep.mode() == "on"
    _put(real_env, "effort_policy=shadow\n")
    assert ep.mode() == "shadow"
    _put(real_env, "maxtabs=3\n")                       # key removed -> default
    assert ep.mode_info() == ("off", "default", False)


def test_file_is_reparsed_only_when_it_changes(real_env, monkeypatch):
    _put(real_env, "effort_policy=on\n")
    ep.mode()
    opens = []

    import builtins
    orig = builtins.open

    def spy(*a, **k):
        opens.append(a[0])
        return orig(*a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    for _ in range(5):
        assert ep.mode() == "on"
    assert opens == []


def test_never_raises_on_unreadable_or_garbage(real_env, monkeypatch):
    real_env.write_bytes(b"\xff\xfe\x00garbage\x80\x81")
    assert ep.mode_info()[0] == "off"
    # a directory where the file should be
    real_env.unlink()
    real_env.mkdir()
    assert ep.mode_info() == ("off", "default", False)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(ep, "_settings_value", boom)
    assert ep.mode_info() == ("off", "default", False)
    assert ep.mode() == "off"


def test_a_raising_log_never_propagates(real_env, monkeypatch):
    _put(real_env, "effort_policy=off\n")
    monkeypatch.setenv("MCP_EFFORT_POLICY", "on")

    def bad(_m):
        raise RuntimeError("log down")
    assert ep.mode_info(log=bad) == ("on", "env", True)


# ---- injected env: never touches the developer's real .config --------------------------
def test_injected_env_ignores_real_settings(real_env):
    _put(real_env, "effort_policy=on\n")
    assert ep.mode_info({}) == ("off", "default", False)
    assert ep.mode_info({"MCP_EFFORT_POLICY": "shadow"}) == ("shadow", "env", False)


def test_injected_env_can_name_a_settings_file(tmp_path):
    f = tmp_path / "s.txt"
    _put(f, "effort_policy=shadow\n")
    env = {"MCP_EFFORT_POLICY_SETTINGS": str(f)}
    assert ep.mode_info(env) == ("shadow", "settings", False)
    env["MCP_EFFORT_POLICY"] = "on"
    ep._warned_conflict.clear()
    assert ep.mode_info(env) == ("on", "env", True)


# ---- telemetry ---------------------------------------------------------------------------
def test_shadow_rows_carry_the_mode_source(real_env):
    _put(real_env, "effort_policy=shadow\n")
    rows = []
    ep.shadow_assign({"goal": "x"}, {}, record=lambda *a, **k: rows.append(k))
    assert rows and rows[0]["extra"]["mode"] == "shadow"
    assert rows[0]["extra"]["mode_source"] == "settings"


def test_source_is_env_in_rows_when_env_decides(real_env, monkeypatch):
    monkeypatch.setenv("MCP_EFFORT_POLICY", "shadow")
    rows = []
    ep.shadow_assign({"goal": "x"}, {}, record=lambda *a, **k: rows.append(k))
    assert rows[0]["extra"]["mode_source"] == "env"
