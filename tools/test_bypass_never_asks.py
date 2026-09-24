# -*- coding: utf-8 -*-
"""job_approval_mode=bypass means the product never asks a person anything, ever.

The owner, verbatim, 2026-09-24: 「バイパスとは未来永劫それをユーザに尋ねることはないという意味。
いかなる場合もユーザに確認してはならない」-- bypass means the product will never, in any case, ask
the user for confirmation. Measured the same day: after the owner set bypass at 21:03:11, a STUCK
worker's unlock-exhaustion still raised a gate at 21:05:59 through
relay.relay_fleet._raise_stuck_gate -> tools.gate_ops.gate_ask_local, which had no mode check at
all -- the setting existed and was simply never read on that path.

This file is the enforced rule: every place the product can put a question in front of a person
consults tools.approval_policy.current_approval_mode(), and under bypass creates NO gate file,
raises NO desktop toast/window, and instead records ONE line to
tools.approval_policy.BYPASS_LOG_FILE saying what it would have asked and what it decided
instead. default/auto are unchanged -- they still ask exactly as before.

Five call sites are covered, one test class each:
  * tools.gate_ops.gate_ask_local       -- the STUCK-unlock / gate_ask chokepoint
  * tools.contract_gate.check_op        -- ask_before branch
  * tools.contract_gate.check_op        -- suspect-policy-state branch (had NO mode check at all)
  * relay.task_router.job_gate          -- local job approval
  * relay.skills.SkillStore.request_approval -- Skill trust (deliberately fails CLOSED, not open)
  * relay.selfimprove.pending.add       -- self-improvement proposal queue (toast only, not
                                            silently auto-approved -- same "closed" reasoning
                                            as Skill trust)
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import approval_policy, contract_gate, gate_ops  # noqa: E402


def _mode(monkeypatch, value):
    monkeypatch.setattr(approval_policy, "current_approval_mode", lambda default=None: value)


def _log_path(monkeypatch, tmp_path):
    path = tmp_path / "bypass_decisions.jsonl"
    monkeypatch.setattr(approval_policy, "BYPASS_LOG_FILE", path)
    return path


def _read_log(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── tools.gate_ops.gate_ask_local ───────────────────────────────────────────────────────────

def test_gate_ask_local_raises_no_gate_under_bypass(monkeypatch, tmp_path):
    monkeypatch.setattr(gate_ops, "GATE_DIR", tmp_path / "gates")
    log = _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "bypass")

    token = gate_ops.gate_ask_local("unlock exhausted after 4 attempts", worker_label="w1")

    assert token is None
    assert not (tmp_path / "gates").exists() or not list((tmp_path / "gates").glob("*.json"))
    rows = _read_log(log)
    assert len(rows) == 1
    assert rows[0]["path"] == "gate_ops.gate_ask_local"
    assert "unlock exhausted" in rows[0]["would_have_asked"]


def test_gate_ask_local_still_raises_a_gate_under_default(monkeypatch, tmp_path):
    monkeypatch.setattr(gate_ops, "GATE_DIR", tmp_path / "gates")
    _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "default")

    token = gate_ops.gate_ask_local("something only a person can answer", notify=False)

    assert token is not None
    assert (tmp_path / "gates" / f"{token}.json").is_file()


# ── tools.contract_gate.check_op: ask_before branch ─────────────────────────────────────────

@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(contract_gate, "GATE_DIR", tmp_path / "gates", raising=False)
    monkeypatch.setattr(contract_gate, "load_contract",
                        lambda *a, **k: {"active": True, "ask_before": ["outbound"],
                                         "stop_when": []}, raising=False)
    monkeypatch.setattr(contract_gate, "policy_state_is_suspect", lambda: "", raising=False)
    monkeypatch.setattr(contract_gate, "_gate_dir", lambda: tmp_path / "gates", raising=False)
    return contract_gate


def test_contract_gate_ask_before_proceeds_without_gate_under_bypass(gate, monkeypatch, tmp_path):
    log = _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "bypass")

    result = gate.check_op("outbound", "send the weekly digest")

    assert result is None  # proceeds
    assert not (tmp_path / "gates").exists() or not list((tmp_path / "gates").glob("*.json"))
    rows = _read_log(log)
    assert any(r["path"] == "contract_gate.check_op(ask_before)" for r in rows)


def test_contract_gate_ask_before_still_asks_under_default(gate, monkeypatch, tmp_path):
    _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "default")

    result = gate.check_op("outbound", "send the weekly digest")

    assert result is not None
    assert list((tmp_path / "gates").glob("*.json"))


# ── tools.contract_gate.check_op: suspect-policy-state branch ──────────────────────────────

def test_contract_gate_suspect_state_proceeds_without_gate_under_bypass(monkeypatch, tmp_path):
    monkeypatch.setattr(contract_gate, "policy_state_is_suspect",
                        lambda: "active_contract.json is corrupt", raising=False)
    monkeypatch.setattr(contract_gate, "_gate_dir", lambda: tmp_path / "gates", raising=False)
    log = _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "bypass")

    result = contract_gate.check_op("delete", "scratch file")

    assert result is None
    assert not (tmp_path / "gates").exists() or not list((tmp_path / "gates").glob("*.json"))
    rows = _read_log(log)
    assert any(r["path"] == "contract_gate.check_op(suspect_policy_state)" for r in rows)


def test_contract_gate_suspect_state_still_asks_under_default(monkeypatch, tmp_path):
    monkeypatch.setattr(contract_gate, "policy_state_is_suspect",
                        lambda: "active_contract.json is corrupt", raising=False)
    monkeypatch.setattr(contract_gate, "_gate_dir", lambda: tmp_path / "gates", raising=False)
    _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "default")

    result = contract_gate.check_op("delete", "scratch file")

    assert result is not None
    assert list((tmp_path / "gates").glob("*.json"))


# ── relay.task_router.job_gate ──────────────────────────────────────────────────────────────

def test_job_gate_allows_without_gate_under_bypass(monkeypatch, tmp_path):
    from relay import task_router
    log = _log_path(monkeypatch, tmp_path)

    decision, why = task_router.job_gate("shell", {"cmd": "echo hi"}, "bypass")

    assert decision == "ALLOW"
    rows = _read_log(log)
    assert any(r["path"] == "task_router.job_gate" for r in rows)


def test_job_gate_still_confirms_under_default_for_a_first_seen_class(monkeypatch, tmp_path):
    from relay import task_router
    _log_path(monkeypatch, tmp_path)

    decision, why = task_router.job_gate("shell", {"cmd": "echo hi"}, "default")

    assert decision == "CONFIRM"


# ── relay.skills.SkillStore.request_approval ────────────────────────────────────────────────

def _write_skill(project_root, name="review-code"):
    root = project_root / "skills"
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: %s\ndescription: Review code\n---\n\nDo the review.\n" % name,
        encoding="utf-8",
    )
    return folder


def test_skill_approval_grants_no_trust_and_raises_no_gate_under_bypass(monkeypatch, tmp_path):
    from pathlib import Path
    from relay.skills import SkillStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _write_skill(tmp_path / "project")
    store = SkillStore(tmp_path / "project", tmp_path / "state.sqlite3", tmp_path / "gates")
    log = _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "bypass")

    review = store.request_approval("review-code")

    assert review["status"] == "bypass-not-asked"
    assert not list((tmp_path / "gates").glob("*.json"))
    skill = store.get("review-code")
    assert skill.trust != "trusted"
    rows = _read_log(log)
    assert any(r["path"] == "relay.skills.SkillStore.request_approval" for r in rows)


def test_skill_approval_still_asks_under_default(monkeypatch, tmp_path):
    from pathlib import Path
    from relay.skills import SkillStore

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _write_skill(tmp_path / "project")
    store = SkillStore(tmp_path / "project", tmp_path / "state.sqlite3", tmp_path / "gates")
    _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "default")

    review = store.request_approval("review-code")

    assert review["status"] == "confirmation-required"
    assert list((tmp_path / "gates").glob("*.json"))


# ── relay.selfimprove.pending.add ───────────────────────────────────────────────────────────

def test_pending_add_does_not_notify_under_bypass_but_stays_queued(monkeypatch, tmp_path):
    from relay.selfimprove import pending

    monkeypatch.setattr(pending, "QUEUE_PATH",
                        str(tmp_path / "pending_decisions.jsonl"), raising=False)
    notified = []
    monkeypatch.setattr(pending, "_notify", lambda *a, **k: notified.append((a, k)))
    log = _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "bypass")

    pid = pending.add(["tools/foo.py"], "proposal reason")

    assert pid  # still queued -- bypass suppresses the toast, not the record
    assert pending.status_of(pid) == pending.OPEN
    assert notified == []
    rows = _read_log(log)
    assert any(r["path"] == "relay.selfimprove.pending.add" for r in rows)


def test_pending_add_still_notifies_under_default(monkeypatch, tmp_path):
    from relay.selfimprove import pending

    monkeypatch.setattr(pending, "QUEUE_PATH",
                        str(tmp_path / "pending_decisions.jsonl"), raising=False)
    notified = []
    monkeypatch.setattr(pending, "_notify", lambda *a, **k: notified.append((a, k)))
    _log_path(monkeypatch, tmp_path)
    _mode(monkeypatch, "default")

    pending.add(["tools/foo.py"], "proposal reason")

    assert len(notified) == 1
