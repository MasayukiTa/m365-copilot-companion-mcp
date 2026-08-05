"""Behaviour of the per-scope folder allow-list, with the OFF case pinned as hard as the ON
case: this ships into installs that are all currently unrestricted, and a policy layer that
quietly narrows what they can reach would be a worse bug than the one it prevents."""
import json

import pytest

from tools import file_ops, folder_policy


@pytest.fixture(autouse=True)
def _isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_policy, "POLICY_FILE", tmp_path / "folder_access.json")
    monkeypatch.setattr(folder_policy, "_CACHE", None, raising=False)
    monkeypatch.setattr(folder_policy, "_CACHE_MTIME", None, raising=False)
    monkeypatch.setattr(folder_policy, "_CACHE_CHECKED", 0.0, raising=False)
    monkeypatch.setattr(folder_policy, "current_scope", lambda: "")
    monkeypatch.setattr(file_ops, "ALLOWED_BASES", None)
    yield


def _write(tmp_path, obj):
    (tmp_path / "folder_access.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    folder_policy.load_policy(force=True)


def test_no_policy_file_leaves_everything_open(tmp_path):
    assert folder_policy.allowed_bases() is None
    file_ops._validate_path(str(tmp_path / "anywhere.txt"))   # must not raise


def test_disabled_policy_is_ignored_even_when_lists_are_present(tmp_path):
    _write(tmp_path, {"enabled": False, "global": [str(tmp_path / "only")]})
    assert folder_policy.allowed_bases() is None
    file_ops._validate_path(str(tmp_path / "elsewhere" / "f.txt"))   # must not raise


def test_enabled_global_list_allows_inside_and_refuses_outside(tmp_path):
    inside = tmp_path / "work"
    inside.mkdir()
    _write(tmp_path, {"enabled": True, "global": [str(inside)]})
    file_ops._validate_path(str(inside / "ok.txt"))
    with pytest.raises(PermissionError):
        file_ops._validate_path(str(tmp_path / "outside.txt"))


def test_scope_list_wins_over_global(tmp_path, monkeypatch):
    lane = tmp_path / "lane"
    common = tmp_path / "common"
    lane.mkdir()
    common.mkdir()
    _write(tmp_path, {"enabled": True, "global": [str(common)],
                      "scopes": {"fleet-w1": [str(lane)]}})
    monkeypatch.setattr(folder_policy, "current_scope", lambda: "fleet-w1")
    file_ops._validate_path(str(lane / "ok.txt"))
    with pytest.raises(PermissionError):
        file_ops._validate_path(str(common / "no.txt"))


def test_scope_without_a_rule_falls_back_to_global(tmp_path, monkeypatch):
    common = tmp_path / "common"
    common.mkdir()
    _write(tmp_path, {"enabled": True, "global": [str(common)],
                      "scopes": {"fleet-w1": [str(tmp_path / "lane")]}})
    monkeypatch.setattr(folder_policy, "current_scope", lambda: "unknown-lane")
    file_ops._validate_path(str(common / "ok.txt"))


def test_enabled_with_no_list_at_all_stays_open(tmp_path, monkeypatch):
    """Turning the switch on must not lock out a scope nobody has written a rule for."""
    _write(tmp_path, {"enabled": True})
    monkeypatch.setattr(folder_policy, "current_scope", lambda: "brand-new-lane")
    assert folder_policy.allowed_bases() is None
    file_ops._validate_path(str(tmp_path / "anything.txt"))


def test_corrupt_policy_file_does_not_lock_the_tools(tmp_path):
    (tmp_path / "folder_access.json").write_text("{not json", encoding="utf-8")
    folder_policy.load_policy(force=True)
    assert folder_policy.allowed_bases() is None
    file_ops._validate_path(str(tmp_path / "anything.txt"))


def test_edits_take_effect_without_a_restart(tmp_path):
    inside = tmp_path / "work"
    inside.mkdir()
    _write(tmp_path, {"enabled": True, "global": [str(inside)]})
    with pytest.raises(PermissionError):
        file_ops._validate_path(str(tmp_path / "outside.txt"))
    _write(tmp_path, {"enabled": False})
    file_ops._validate_path(str(tmp_path / "outside.txt"))


def test_env_floor_still_applies_when_a_scope_widens(tmp_path, monkeypatch):
    """MCP_ALLOWED_BASE keeps its meaning: a scope list can narrow it, never widen it."""
    floor = tmp_path / "floor"
    (floor / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(file_ops, "ALLOWED_BASES", [floor.resolve()])
    _write(tmp_path, {"enabled": True, "global": [str(floor / "sub"), str(outside)]})
    file_ops._validate_path(str(floor / "sub" / "ok.txt"))
    with pytest.raises(PermissionError):
        file_ops._validate_path(str(outside / "no.txt"))


def test_describe_explains_what_applied(tmp_path, monkeypatch):
    lane = tmp_path / "lane"
    lane.mkdir()
    _write(tmp_path, {"enabled": True, "scopes": {"fleet-w1": [str(lane)]}})
    monkeypatch.setattr(folder_policy, "current_scope", lambda: "fleet-w1")
    d = folder_policy.describe()
    assert d["enabled"] is True and d["scope"] == "fleet-w1"
    assert d["matched"] == "scope" and d["restricted"] is True
    assert str(lane.resolve()) in d["allowed"]
