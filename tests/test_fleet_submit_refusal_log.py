"""fleet_submit_refusals.jsonl: each refusal leaves exactly one line, replies are unchanged."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import fleet_intake as FI  # noqa: E402
from tools import obs_log, tool_ledger  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_ledger, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"))
    monkeypatch.setattr(obs_log, "_WARNED", False)
    monkeypatch.setattr(FI, "_duplicate_of", lambda text: None)
    monkeypatch.setattr(FI, "_pending_count", lambda: 0)
    return tmp_path


def _rows(state):
    p = state / obs_log.REFUSALS_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def test_missing_goal(state):
    out = FI.fleet_submit("   ", source="phone")
    assert out.startswith("[fleet_submit: refused -- an empty goal")
    (r,) = _rows(state)
    assert r["reason"] == "missing_goal" and r["goal_len"] == 0 and r["source"] == "phone"


def test_too_long(state):
    goal = "word\n" * 2000
    out = FI.fleet_submit(goal, source="s" * 200)
    assert "over the %d limit" % FI.MAX_GOAL_CHARS in out
    (r,) = _rows(state)
    assert r["reason"] == "too_long" and r["goal_len"] == len(" ".join(goal.split()))
    assert "\n" not in r["goal_head"] and len(r["goal_head"]) == 80
    assert len(r["source"]) == 80


def test_local_loop(state, monkeypatch):
    monkeypatch.setattr(FI.TR, "is_local_loop_control_submission", lambda t, s: True)
    out = FI.fleet_submit("RUN something", source="x")
    assert "LOCAL_LOOP" in out
    (r,) = _rows(state)
    assert r["reason"] == "local_loop"


def test_similar_carries_the_matching_id(state, monkeypatch):
    monkeypatch.setattr(FI, "_duplicate_of", lambda text: ("job123abc", 0.9))
    out = FI.fleet_submit("do the same thing again please", source="x")
    assert "job123abc" in out and "90%" in out
    (r,) = _rows(state)
    assert r["reason"] == "similar" and r["similar_to"] == "job123abc"


def test_pending_full(state, monkeypatch):
    monkeypatch.setattr(FI, "_pending_count", lambda: FI.MAX_PENDING)
    out = FI.fleet_submit("a goal", source="x")
    assert "already waiting" in out
    (r,) = _rows(state)
    assert r["reason"] == "pending_full"


def test_other_when_the_queue_cannot_be_written(state, monkeypatch):
    def boom():
        raise OSError("read-only")

    monkeypatch.setattr(FI.TR, "ensure_dirs", boom)
    out = FI.fleet_submit("a goal", source="x")
    assert out.startswith("[fleet_submit: could not queue")
    (r,) = _rows(state)
    assert r["reason"] == "other"


def test_address_shapes_are_not_logged(state):
    FI.fleet_submit("", source="from someone@example.com via phone")
    text = (state / obs_log.REFUSALS_NAME).read_text("utf-8")
    assert "someone@example.com" not in text


def test_logging_failure_does_not_change_the_reply(state, monkeypatch):
    def boom():
        raise OSError("disk on fire")

    monkeypatch.setattr(obs_log, "_state_dir", boom)
    out = FI.fleet_submit("", source="x")
    assert out == "[fleet_submit: refused -- an empty goal is not an instruction]"
