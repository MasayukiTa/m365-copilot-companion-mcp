import json

from bench.local_review_transport import (
    _campaign_snapshot,
    _consume_console_commands,
    build_local_review_job,
)
from relay.local_job_store import LocalJobStore


def _goal():
    return {
        "text": "Review x.py and emit <<<FINDINGS>>> JSON <<<END_FINDINGS>>>",
        "cwd": "C:/repo",
        "task_id": "security-auth-0001",
    }


def test_build_job_has_fixed_read_only_three_pass_plan():
    job = build_local_review_job(_goal(), "deep_test_0001")
    assert job["execution_profile"] == "LOCAL_LOOP"
    assert ["PRODUCER", "ADVERSARIAL", "ADJUDICATION"] == [
        step["instruction"].split()[0] for step in job["turn_plan"]
    ]
    assert len(job["turn_plan"]) == 3
    assert all("Do not edit source files" in step["instruction"] for step in job["turn_plan"])
    assert job["constraints"]["allowed_base"] == "C:/repo"
    assert job["constraints"]["allow_network"] is False
    assert job["constraints"]["max_turns"] == 3
    assert job["constraints"]["max_attempts"] == 5
    assert job["constraints"]["continue_on_unsafe_abort"] is True
    assert all("P2C SAFE-PROGRESS POLICY" in step["instruction"] for step in job["turn_plan"])
    assert all("Do not abort the entire review" in step["instruction"]
               for step in job["turn_plan"])


def test_campaign_snapshot_projects_final_sqlite_summary_without_transcript(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = build_local_review_job(_goal(), "deep_test_0001")
    store.create_job(job, now=1)
    for seq in (1, 2, 3):
        claim = store.claim_turn("deep_test_0001", seq, "agent", now=seq * 10)
        status = "CANDIDATE_DONE" if seq == 3 else "CONTINUE"
        summary = "<<<FINDINGS>>>\n[]\n<<<END_FINDINGS>>>" if seq == 3 else "progress"
        store.commit_turn(
            "deep_test_0001", seq, claim["lease_id"], claim["fencing_token"],
            status, summary, now=seq * 10 + 1,
        )
    store.verify_candidate("deep_test_0001", True, "verified", now=40)
    snapshot = _campaign_snapshot(store, [{
        "job_id": "deep_test_0001", "worker": "w0", "goal": _goal(),
    }], started=1, active_workers=set())
    worker = snapshot["workers"][0]
    assert worker["outcome"] == "DONE"
    assert worker["last"].startswith("<<<FINDINGS>>>")
    assert worker["display_result"] == worker["last"]
    assert worker["transcript"] == ""
    assert snapshot["response_content_reads"] == 0
    assert snapshot["open_tabs"] == 0
    json.dumps(snapshot)


def test_console_stop_cancels_campaign_job(tmp_path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    job = build_local_review_job(_goal(), "deep_test_0001")
    store.create_job(job)
    commands = tmp_path / "commands.json"
    commands.write_text(json.dumps({"close": ["w0"]}), encoding="utf-8")
    stopped = _consume_console_commands(commands, store, [{
        "job_id": "deep_test_0001", "worker": "w0", "goal": _goal(),
    }])
    assert stopped == {"w0"}
    assert store.get_job_status("deep_test_0001")["status"] == "CANCELLED"
    assert not commands.exists()
