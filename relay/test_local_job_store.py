import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from relay.local_job_store import JobStoreError, LocalJobStore


def _job(job_id="job_1"):
    return {
        "job_id": job_id,
        "execution_profile": "LOCAL_LOOP",
        "data_location": "LOCAL",
        "requires_local_tool": True,
        "task": {"type": "code_fix", "instruction": "Fix the failing test"},
        "constraints": {"allowed_base": "C:/work", "max_claim_bytes": 8192},
        "acceptance_checks": [{"type": "pytest", "args": "-q"}],
    }


def _store(tmp_path):
    return LocalJobStore(tmp_path / "jobs.sqlite3")


def test_create_claim_continue_and_compact_previous_summary(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=10)
    c1 = store.claim_turn("job_1", 1, "worker-a", now=11)
    assert c1["instruction"] == "Fix the failing test"
    out = store.commit_turn(
        "job_1", 1, c1["lease_id"], c1["fencing_token"],
        "CONTINUE", "fixed one issue", "Run the remaining test", now=12,
    )
    assert out["next_seq"] == 2
    assert store.get_job_status("job_1")["commit"]["summary"] == "fixed one issue"
    c2 = store.claim_turn("job_1", 2, "worker-b", now=13)
    assert c2["context"]["previous_summary"] == "fixed one issue"
    assert c2["instruction"] == "Run the remaining test"


def test_claim_is_exclusive_and_expired_worker_is_fenced(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=0)
    first = store.claim_turn("job_1", 1, "old", lease_seconds=30, now=1)
    with pytest.raises(JobStoreError) as active:
        store.claim_turn("job_1", 1, "other", now=2)
    assert active.value.code == "LEASE_ACTIVE"
    replacement = store.claim_turn("job_1", 1, "new", lease_seconds=30, now=32)
    assert replacement["fencing_token"] == first["fencing_token"] + 1
    with pytest.raises(JobStoreError) as stale:
        store.commit_turn(
            "job_1", 1, first["lease_id"], first["fencing_token"],
            "CANDIDATE_DONE", "old result", now=33,
        )
    assert stale.value.code in {"LEASE_MISMATCH", "FENCE_MISMATCH"}


def test_two_simultaneous_claims_produce_one_lease(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    LocalJobStore(path).create_job(_job(), now=0)

    def claim(worker):
        try:
            return LocalJobStore(path).claim_turn("job_1", 1, worker, now=1)
        except JobStoreError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["a", "b"]))
    assert sum(isinstance(x, dict) and x.get("ok") for x in results) == 1
    assert "LEASE_ACTIVE" in results


def test_commit_is_idempotent_but_conflicting_payload_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=0)
    claim = store.claim_turn("job_1", 1, "w", now=1)
    args = ("job_1", 1, claim["lease_id"], claim["fencing_token"])
    first = store.commit_turn(*args, "CANDIDATE_DONE", "complete", now=2)
    second = store.commit_turn(*args, "CANDIDATE_DONE", "complete", now=3)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    with pytest.raises(JobStoreError) as conflict:
        store.commit_turn(*args, "CANDIDATE_DONE", "different", now=3)
    assert conflict.value.code == "COMMIT_CONFLICT"


def test_candidate_done_requires_local_verification(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=0)
    claim = store.claim_turn("job_1", 1, "w", now=1)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "CANDIDATE_DONE", "looks done", now=2,
    )
    assert store.get_job_status("job_1")["status"] == "VERIFYING"
    failed = store.verify_candidate("job_1", False, "1 failed", now=3)
    assert failed["next_seq"] == 2
    c2 = store.claim_turn("job_1", 2, "w2", now=4)
    assert "1 failed" in c2["instruction"]
    store.commit_turn(
        "job_1", 2, c2["lease_id"], c2["fencing_token"],
        "CANDIDATE_DONE", "fixed", now=5,
    )
    store.verify_candidate("job_1", True, "pytest passed", now=6)
    assert store.get_job_status("job_1")["status"] == "DONE"


def test_profile_mismatch_and_payload_limits_fail_closed(tmp_path):
    store = _store(tmp_path)
    cloud = _job("cloud")
    cloud.update(execution_profile="CLOUD_WORKIQ", data_location="SHAREPOINT",
                 requires_local_tool=False)
    with pytest.raises(JobStoreError) as mismatch:
        store.create_job(cloud)
    assert mismatch.value.code == "PROFILE_MISMATCH"
    large = _job("large")
    large["constraints"]["max_claim_bytes"] = 4
    with pytest.raises(JobStoreError) as too_large:
        store.create_job(large)
    assert too_large.value.code == "PAYLOAD_TOO_LARGE"


def test_retryable_abort_releases_lease_and_restart_recovers(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = LocalJobStore(path)
    store.create_job(_job(), now=0)
    claim = store.claim_turn("job_1", 1, "w", now=1)
    store.abort_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "NETWORK", "temporary", True, now=2,
    )
    reopened = LocalJobStore(path)
    retry = reopened.claim_turn("job_1", 1, "w2", now=3)
    assert retry["fencing_token"] == claim["fencing_token"] + 1
    assert reopened.get_job_status("job_1")["retry_count"] == 1


def test_console_projection_uses_committed_summary_not_web_transcript(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=0)
    claim = store.claim_turn("job_1", 1, "w", now=1)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "WAITING_USER", "Need operator input", artifacts=[{"path": "a.txt"}], now=2,
    )
    snapshot = store.console_snapshot()
    worker = snapshot["workers"][0]
    assert worker["last"] == "Need operator input"
    assert worker["transcript"] == ""
    assert worker["artifacts"] == [{"path": "a.txt"}]
    json.dumps(snapshot)
    assert store.checkpoint()["ok"] is True
