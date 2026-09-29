from __future__ import annotations

import json
from pathlib import Path

from relay import task_router as tr

CONTROL = "RUN companion_20260929_x seq=1 worker=local_abc"
WRAPPER = "LOCAL_LOOP job companion_20260929_x seq=1 worker=local_abc: claim and execute the operator-authored turn"


def test_autostart_refuses_control_before_writing_goals_or_launching(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tr, "_agent_url", lambda: "https://example.invalid")
    out = tr.autostart_fleet([{"text": CONTROL, "jid": "j1"}], str(tmp_path), launcher=lambda cmd: called.append(cmd) or 1)
    assert out.get("ok") is False
    assert out.get("refused") is True
    assert called == []
    assert not (tmp_path / "autostart.goals.jsonl").exists()


def test_fleet_handoff_refuses_control_before_live_or_cold_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "fleet_is_live", lambda state_dir=None: True)
    monkeypatch.setattr(tr, "add_goal_to_live_fleet", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deliver")))
    status, result = tr.fleet_handoff(WRAPPER, "j2", str(tmp_path))
    assert status == "refused"
    assert result.get("refused") is True


def test_waiting_control_residue_is_consumed_as_refused_not_retried_forever(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(tr, "TASKS", str(tasks))
    monkeypatch.setattr(tr, "AUTOSTART", False)
    monkeypatch.setattr(tr, "fleet_is_live", lambda state_dir=None: False)
    tr.ensure_dirs()
    p = tasks / "for_fleet" / "oldcontrol.txt"
    p.write_text(CONTROL, encoding="utf-8")

    out = tr._deliver_waiting_goals(now_ts=123.0, state_dir=str(tmp_path / "fleet"))
    assert not p.exists(), "stale control residue must not survive every supervisor tick"
    assert out and out[0]["status"] == "refused"
    assert out[0]["result"]["refused"] is True
    audit = tasks / "done" / "oldcontrol.delivered.json"
    assert audit.is_file()
    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "refused"
