# -*- coding: utf-8 -*-
"""relay/result_cache.py: shadow-only, read-only tool result caching that records, never serves.

These are the module's own behavioural tests (the sibling of relay/test_load_routing.py). The
cross-layer agreement with the registry, the cockpit and the UI driver is checked separately in
tests/test_result_cache_agrees.py.
"""
from __future__ import annotations

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import result_cache as RC  # noqa: E402


def _settings(tmp_path, value):
    p = tmp_path / "settings.txt"
    p.write_text("result_cache=%s\n" % value, encoding="utf-8")
    return str(p)


def _rows(tmp_path):
    f = tmp_path / ".fleet" / "result_cache_shadow.jsonl"
    if not f.exists():
        return []
    return [json.loads(ln) for ln in io.open(str(f), encoding="utf-8") if ln.strip()]


def _point_log(tmp_path, monkeypatch):
    monkeypatch.setattr(RC, "LOG", str(tmp_path / ".fleet" / "result_cache_shadow.jsonl"))


# ------------------------------------------------------------------ the setting
def test_mode_reads_off_and_shadow_and_nothing_else(tmp_path):
    RC._reset_for_test()
    assert RC.mode(_settings(tmp_path, "shadow")) == "shadow"
    RC._reset_for_test()
    assert RC.mode(_settings(tmp_path, "off")) == "off"
    RC._reset_for_test()
    # a value that would serve from the cache does not exist: it reads as off
    assert RC.mode(_settings(tmp_path, "on")) == "off"
    RC._reset_for_test()
    assert RC.mode(_settings(tmp_path, "serve")) == "off"


def test_modes_tuple_is_off_shadow_only():
    assert RC.MODES == ("off", "shadow")
    assert RC.KEY == "result_cache"


# ------------------------------------------------------------------ off records nothing
def test_off_writes_no_file_and_counts_nothing(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "off")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.2, settings_path=sp)
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.2, settings_path=sp)
    assert _rows(tmp_path) == []
    assert RC.status_block(sp)["result_cache"]["seen"] == 0


# ------------------------------------------------------------------ first sighting is a miss
def test_first_call_is_a_miss_second_identical_call_is_a_hit(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.5, settings_path=sp, now=100.0)
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.5, settings_path=sp, now=101.0)
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]["hit"] is False and rows[0]["saved_s"] is None
    assert rows[1]["hit"] is True and rows[1]["matched"] is True
    assert rows[1]["saved_s"] == 0.5
    blk = RC.status_block(sp)["result_cache"]
    assert blk == {"mode": "shadow", "seen": 2, "hits": 1, "differed": 0}


def test_a_hit_whose_result_changed_is_differed_not_matched(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.3, settings_path=sp, now=100.0)
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 2}, read_only=True,
                      duration_s=0.3, settings_path=sp, now=101.0)
    rows = _rows(tmp_path)
    assert rows[1]["hit"] is True
    assert rows[1]["matched"] is False and rows[1]["differed"] is True
    assert RC.status_block(sp)["result_cache"]["differed"] == 1


def test_an_expired_entry_is_a_fresh_miss(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.1, settings_path=sp, now=100.0)
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.1, settings_path=sp, now=100.0 + RC.TTL_S + 1.0)
    rows = _rows(tmp_path)
    assert rows[1]["hit"] is False
    assert RC.status_block(sp)["result_cache"]["hits"] == 0


def test_a_write_or_unknown_tool_is_never_a_candidate(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("write_file", {"path": "a"}, {"ok": 1}, read_only=False,
                      duration_s=0.2, settings_path=sp, now=100.0)
    RC.shadow_observe("write_file", {"path": "a"}, {"ok": 1}, read_only=False,
                      duration_s=0.2, settings_path=sp, now=101.0)
    assert _rows(tmp_path) == []
    assert RC.status_block(sp)["result_cache"]["seen"] == 0


def test_different_arguments_are_different_questions(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.2, settings_path=sp, now=100.0)
    RC.shadow_observe("read_file", {"path": "b"}, {"x": 1}, read_only=True,
                      duration_s=0.2, settings_path=sp, now=101.0)
    rows = _rows(tmp_path)
    assert rows[0]["hit"] is False and rows[1]["hit"] is False


def test_the_row_holds_no_argument_values_or_result_content(tmp_path, monkeypatch):
    RC._reset_for_test()
    _point_log(tmp_path, monkeypatch)
    sp = _settings(tmp_path, "shadow")
    secret_arg = "SECRET_PATH_VALUE"
    secret_res = "SECRET_RESULT_BODY"
    RC.shadow_observe("read_file", {"path": secret_arg}, {"body": secret_res},
                      read_only=True, duration_s=0.2, settings_path=sp, now=100.0)
    blob = io.open(str(tmp_path / ".fleet" / "result_cache_shadow.jsonl"),
                   encoding="utf-8").read()
    assert secret_arg not in blob and secret_res not in blob
    assert "read_file" in blob  # the tool name is allowed


def test_a_write_error_is_swallowed_not_raised(tmp_path, monkeypatch):
    RC._reset_for_test()
    # point LOG at a path whose parent is a file, so makedirs/append fail
    bad = tmp_path / "afile"
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setattr(RC, "LOG", str(bad / "nested" / "log.jsonl"))
    sp = _settings(tmp_path, "shadow")
    RC.shadow_observe("read_file", {"path": "a"}, {"x": 1}, read_only=True,
                      duration_s=0.2, settings_path=sp, now=100.0)
    assert RC._STATE["write_errors"] >= 1


def test_decide_is_pure():
    assert RC.decide(None, 100.0) == {"hit": False, "age": None}
    assert RC.decide({"at": 100.0, "digest": "d"}, 100.5)["hit"] is True
    assert RC.decide({"at": 100.0, "digest": "d"}, 100.0 + RC.TTL_S)["hit"] is False


def test_status_block_shape_before_any_call(tmp_path):
    RC._reset_for_test()
    blk = RC.status_block(_settings(tmp_path, "off"))
    assert blk == {"result_cache": {"mode": "off", "seen": 0, "hits": 0, "differed": 0}}
