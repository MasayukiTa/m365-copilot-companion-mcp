import json
from pathlib import Path

from bench import local_review_transport as transport
from bench.local_review_transport import (
    CAMPAIGN_MANIFEST,
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
    assert job["constraints"]["p2c_level"] == 1
    assert all("P2C SAFE-PROGRESS POLICY" in step["instruction"] for step in job["turn_plan"])
    assert all("Do not abort the entire review" in step["instruction"]
               for step in job["turn_plan"])


def test_level_two_requires_active_validation_and_forbids_false_clean_fallback():
    goal = _goal()
    goal["metadata"] = {"p2c_level": 2}
    job = build_local_review_job(goal, "deep_test_full_0001")
    assert job["constraints"]["p2c_level"] == 2
    assert job["constraints"]["require_active_validation"] is True
    assert all("P2C LEVEL 2 ACTIVE-VALIDATION POLICY" in step["instruction"]
               for step in job["turn_plan"])
    assert "Static inspection must never be presented" in job["turn_plan"][0]["instruction"]


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


def test_campaign_manifest_reopens_same_sqlite_jobs(tmp_path):
    state = tmp_path / "state"
    jobs_dir = state / "local_jobs"
    jobs_dir.mkdir(parents=True)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")

    first, started, resumed = transport._campaign_entries(
        state, jobs_dir, store, [_goal()],
    )
    second, resumed_started, resumed = transport._campaign_entries(
        state, jobs_dir, store, [_goal()],
    )

    assert resumed is True
    assert resumed_started == started
    assert second[0]["job_id"] == first[0]["job_id"]
    assert len(store.list_job_statuses()) == 1
    assert (state / CAMPAIGN_MANIFEST).is_file()


def test_campaign_manifest_recovers_jobs_missing_after_bootstrap_crash(tmp_path):
    state = tmp_path / "state"
    jobs_dir = state / "local_jobs"
    jobs_dir.mkdir(parents=True)
    db = tmp_path / "jobs.sqlite3"
    first_store = LocalJobStore(db)
    first, started, _ = transport._campaign_entries(
        state, jobs_dir, first_store, [_goal()],
    )
    job_id = first[0]["job_id"]

    # Model power loss after the durable manifest was replaced but before its SQLite
    # transaction committed: the surviving manifest must recreate the same id.
    db.unlink()
    recovered_store = LocalJobStore(db)
    recovered, recovered_started, resumed = transport._campaign_entries(
        state, jobs_dir, recovered_store, [_goal()],
    )

    assert resumed is True
    assert recovered_started == started
    assert recovered[0]["job_id"] == job_id
    assert recovered_store.get_job_status(job_id)["status"] == "READY"


def test_unexpected_controller_exit_restarts_same_job_until_done(tmp_path, monkeypatch):
    goals = tmp_path / "goals.jsonl"
    goals.write_text(json.dumps(_goal(), ensure_ascii=False) + "\n", encoding="utf-8")
    state = tmp_path / "state"
    db = tmp_path / "jobs.sqlite3"
    launches = []

    class FakeController:
        next_pid = 9000

        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.pid = FakeController.next_pid
            FakeController.next_pid += 1
            self.returncode = 7 if not launches else 0
            launches.append(self)
            if self.returncode == 0:
                job_file = Path(cmd[cmd.index("--job-file") + 1])
                job_id = json.loads(job_file.read_text(encoding="utf-8"))["job_id"]
                current = LocalJobStore(db)
                for seq in (1, 2, 3):
                    claim = current.claim_turn(job_id, seq, "replacement")
                    current.commit_turn(
                        job_id, seq, claim["lease_id"], claim["fencing_token"],
                        "CANDIDATE_DONE" if seq == 3 else "CONTINUE",
                        "final" if seq == 3 else "progress",
                    )
                current.verify_candidate(job_id, True, "verified")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    monkeypatch.setenv("MCP_EXECUTION_PROFILES", "1")
    monkeypatch.setenv("MCP_LOCAL_JOB_DB", str(db))
    monkeypatch.setenv("MCP_LOCAL_CONTROLLER_RESTART_BACKOFF_MAX", "1")
    monkeypatch.setattr(transport.subprocess, "Popen", FakeController)

    result = transport.run_local_review_fleet(
        str(goals), 1, str(state), repo_root=str(tmp_path), python_exe="python",
    )

    assert result == 0
    assert len(launches) == 2
    manifest = json.loads((state / CAMPAIGN_MANIFEST).read_text(encoding="utf-8"))
    job_id = manifest["entries"][0]["job_id"]
    status = LocalJobStore(db).get_job_status(job_id, event_limit=30)
    assert status["status"] == "DONE"
    assert any(event["event"] == "CONTROLLER_RESTART_SCHEDULED"
               for event in status["events"])

    # Re-entering the same state directory is an instant no-op, not a duplicate campaign.
    assert transport.run_local_review_fleet(
        str(goals), 1, str(state), repo_root=str(tmp_path), python_exe="python",
    ) == 0
    assert len(launches) == 2
