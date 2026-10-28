"""Recovery must distinguish an unknown crash from an actual repeated crash."""
import json
from pathlib import Path

from relay import fleet_reaper, fleet_resume


def test_unknown_legacy_signature_does_not_block_recovery():
    record = {"state": "pending", "resume": {"count": 2, "last_ts": 1,
              "last_signature": "0", "last_free_bytes": 100}}
    assert fleet_resume.resume_gate(record, 10000, 90, 0, "0") == (True, "ok")
    record["resume"]["last_signature"] = "real-crash"
    assert fleet_resume.resume_gate(record, 10000, 90, 0, "real-crash") == (False, "same_crash_no_more_space")


def test_supervisor_transfers_empty_signature_as_json():
    script = (Path(__file__).resolve().parent.parent / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")
    assert "print(json.dumps({'signature': s, 'enospc': e}))" in script
    assert "$sigData.enospc -eq $true" in script
    assert '$Signature -ne "0"' in script


def test_long_running_resume_gets_new_retry_budget(tmp_path, monkeypatch):
    folder = tmp_path / "interrupted"
    folder.mkdir()
    (folder / "parent.json").write_text(json.dumps({"resume": {"count": 3, "history": [1]}}))
    monkeypatch.setattr(fleet_reaper.time, "time", lambda: 2000)
    marker = {"resume_lineage": "parent", "start_ts": 1000}
    result = fleet_reaper._snapshot_payload({}, marker, {}, "new", str(tmp_path))
    assert result["resume"]["count"] == 0
    assert result["resume"]["lineage"] == "parent"
    marker["start_ts"] = 1999
    assert fleet_reaper._snapshot_payload({}, marker, {}, "new", str(tmp_path))["resume"]["count"] == 3
