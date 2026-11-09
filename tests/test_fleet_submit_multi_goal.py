# -*- coding: utf-8 -*-
"""fleet_submit_multigoal.jsonl: a prompt packing SEVERAL INDEPENDENT GOALS is NOTED, never refused.

Owner rule 2026-09-12 (jsonl L6695): a real person types one instruction, not a numbered battery
of them. Following the same owner instruction that governs similarity (never refuse, only record),
such a submission is QUEUED whole and the count is recorded in fleet_submit_multigoal.jsonl --
nothing lands in the refusals log.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import fleet_intake as FI  # noqa: E402
from tools import obs_log, tool_ledger  # noqa: E402

BATTERY = "1. \u58f2\u4e0a\u30c7\u30fc\u30bf\u3092\u96c6\u8a08\u3057\u3066\u30ec\u30dd\u30fc\u30c8\u306b\u307e\u3068\u3081\u308b\n2. \u5728\u5eab\u8868\u3092\u66f4\u65b0\u3057\u3066\u5dee\u5206\u3092\u51fa\u3059\n3. \u9867\u5ba2\u540d\u7c3f\u3092\u6d17\u3063\u3066\u91cd\u8907\u3092\u6d88\u3059"
SINGLE = "\u58f2\u4e0a\u30c7\u30fc\u30bf\u3092\u96c6\u8a08\u3057\u3066\u3001\u5728\u5eab\u8868\u3068\u7a81\u304d\u5408\u308f\u305b\u3066\u30ec\u30dd\u30fc\u30c8\u306b\u307e\u3068\u3081\u3066\u304f\u3060\u3055\u3044\u3002"
SINGLE2 = "\u58f2\u4e0a\u30c7\u30fc\u30bf\u3092\u96c6\u8a08\u3057\u3066\u30ec\u30dd\u30fc\u30c8\u306b\u307e\u3068\u3081\u3066\u304f\u3060\u3055\u3044"
NONCONSEC = "1. \u307e\u305a\u8abf\u67fb\u3059\u308b\u3068\u3044\u3046\u8a71\u3060\u304c\u3001step 3) \u306f\u3042\u3068\u56de\u3057\u3067\u3088\u3044\u3068\u8003\u3048\u3066\u3044\u308b"
FW = "\uff11\uff0e\u6700\u521d\u306e\u8abf\u67fb\u3092\u3057\u3063\u304b\u308a\u3068\u9032\u3081\u308b\n\uff12\uff0e\u6b21\u306e\u5b9f\u88c5\u3092\u4e01\u5be7\u306b\u4ed8\u3051\u52a0\u3048\u308b"
CONT = "1. \u58f2\u4e0a\u30c7\u30fc\u30bf\u3092\u96c6\u8a08\u3057\u3066\u30ec\u30dd\u30fc\u30c8\u306b\u307e\u3068\u3081\u308b\n2. \u5728\u5eab\u8868\u3092\u66f4\u65b0\u3057\u3066\u5dee\u5206\u3092\u51fa\u3059\n3. \u9867\u5ba2\u540d\u7c3f\u3092\u6d17\u3063\u3066\u91cd\u8907\u3092\u6d88\u3059"


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_ledger, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"))
    monkeypatch.setattr(obs_log, "_WARNED", False)
    monkeypatch.setattr(FI, "_duplicate_of", lambda text: None)
    monkeypatch.setattr(FI, "_pending_count", lambda: 0)
    monkeypatch.setattr(FI.TR, "TASKS", str(tmp_path / "tasks"))
    FI.TR.ensure_dirs()
    return tmp_path


def _multigoal_rows(state):
    p = state / obs_log.MULTI_GOAL_OBSERVED_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _refusal_rows(state):
    p = state / obs_log.REFUSALS_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def test_detects_a_consecutive_numbered_battery():
    assert FI._count_multiple_goals(BATTERY) == 3


def test_single_instruction_is_not_a_battery():
    assert FI._count_multiple_goals(SINGLE) == 0


def test_non_consecutive_numbers_are_not_a_battery():
    assert FI._count_multiple_goals(NONCONSEC) == 0


def test_short_numbered_items_are_not_a_battery():
    assert FI._count_multiple_goals("1. yes\n2. no\n3. maybe") == 0


def test_fullwidth_numbers_are_detected():
    assert FI._count_multiple_goals(FW) == 2


def test_a_multi_goal_prompt_is_queued_and_recorded(state):
    out = FI.fleet_submit(BATTERY, source="phone")
    assert "queued" in out and "refused" not in out
    assert _refusal_rows(state) == []
    (r,) = _multigoal_rows(state)
    assert r["goal_count"] == 3 and r["queued"] is True
    assert r["source"] == "phone"


def test_a_single_goal_records_nothing(state):
    out = FI.fleet_submit(SINGLE2, source="phone")
    assert "queued" in out
    assert _multigoal_rows(state) == []


def test_a_continuation_is_exempt(state):
    # CONT is a real battery; resume_conv is what exempts it, proving the carve-out works.
    assert FI._count_multiple_goals(CONT) >= FI.MULTI_GOAL_MIN
    out = FI.fleet_submit(CONT, source="phone", resume_conv="sess:abc123")
    assert "queued" in out
    assert _multigoal_rows(state) == []
    assert _refusal_rows(state) == []


def test_recording_failure_does_not_change_the_reply(state, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(obs_log, "log_multi_goal_observed", boom)
    out = FI.fleet_submit(BATTERY, source="phone")
    assert "queued" in out and "refused" not in out
