import pytest

from relay.local_job_store import JobStoreError, LocalJobStore


def _job(job_id="job_1", *, plan=None):
    job = {
        "job_id": job_id,
        "execution_profile": "LOCAL_LOOP",
        "data_location": "LOCAL",
        "requires_local_tool": True,
        "task": {"type": "companion_task", "instruction": "Prepare the report"},
        "constraints": {"allowed_base": "C:/work", "max_claim_bytes": 8192},
        "acceptance_checks": [],
    }
    if plan is not None:
        job["turn_plan"] = [{"instruction": text} for text in plan]
    return job


def _store(tmp_path):
    return LocalJobStore(tmp_path / "jobs.sqlite3")


def _texts(claim):
    return [row["text"] for row in claim["context"]["operator_steers"]]


def test_ready_steer_is_durable_and_delivered_on_claim(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)

    queued = store.queue_operator_steer("job_1", "Only use the Q3 figures", now=2)
    assert queued["pending"] == 1
    assert store.get_job_status("job_1")["operator_steer_pending"] == 1

    claim = store.claim_turn("job_1", 1, "worker-a", now=3)
    assert claim["instruction"] == "Prepare the report"
    assert _texts(claim) == ["Only use the Q3 figures"]
    assert store.get_job_status("job_1")["operator_steer_pending"] == 0

    events = store.get_job_status("job_1", event_limit=20)["events"]
    assert any(e["event"] == "OPERATOR_STEER_QUEUED" for e in events)
    applied = [e for e in events if e["event"] == "OPERATOR_STEER_APPLIED"]
    assert applied and applied[-1]["payload"]["count"] == 1


def test_steer_arriving_during_active_lease_waits_for_next_sequence(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    first = store.claim_turn("job_1", 1, "worker-a", now=2)
    assert _texts(first) == []

    store.queue_operator_steer("job_1", "Drop appendix B", now=3)
    # The already-issued claim is immutable.  Its correction is still pending in SQLite.
    assert store.get_job_status("job_1")["operator_steer_pending"] == 1

    store.commit_turn(
        "job_1", 1, first["lease_id"], first["fencing_token"],
        "CONTINUE", "drafted body", "Review the draft", now=4,
    )
    second = store.claim_turn("job_1", 2, "worker-b", now=5)
    assert second["instruction"] == "Review the draft"
    assert _texts(second) == ["Drop appendix B"]


def test_retry_of_same_logical_sequence_receives_the_same_applied_steer(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    store.queue_operator_steer("job_1", "Use the corrected source file", now=2)

    first = store.claim_turn("job_1", 1, "worker-a", now=3)
    assert _texts(first) == ["Use the corrected source file"]
    store.retry_uncommitted_turn("job_1", 1, "browser restarted", now=4)

    replacement = store.claim_turn("job_1", 1, "worker-b", now=5)
    assert replacement["fencing_token"] == first["fencing_token"] + 1
    assert _texts(replacement) == ["Use the corrected source file"]


def test_fixed_plan_remains_authoritative_while_steer_is_an_overlay(tmp_path):
    store = _store(tmp_path)
    plan = ["Inspect source A", "Inspect source B"]
    store.create_job(_job(plan=plan), now=1)
    store.queue_operator_steer("job_1", "For source A, ignore archived rows", now=2)

    first = store.claim_turn("job_1", 1, "worker-a", now=3)
    assert first["instruction"] == "Inspect source A"
    assert first["turn_number"] == 1 and first["turn_total"] == 2
    assert _texts(first) == ["For source A, ignore archived rows"]

    store.commit_turn(
        "job_1", 1, first["lease_id"], first["fencing_token"],
        "CONTINUE", "source A checked", "MODEL MUST NOT REPLACE THE PLAN", now=4,
    )
    second = store.claim_turn("job_1", 2, "worker-b", now=5)
    assert second["instruction"] == "Inspect source B"
    assert _texts(second) == []


def test_waiting_runtime_can_collect_a_steer_without_bypassing_the_pause(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    store.mark_waiting_runtime("job_1", "repair the connector", now=2)

    queued = store.queue_operator_steer("job_1", "Once resumed, use the cached export", now=3)
    assert queued["status"] == "WAITING_RUNTIME"
    assert store.get_job_status("job_1")["status"] == "WAITING_RUNTIME"

    store.resume_runtime("job_1", now=4)
    claim = store.claim_turn("job_1", 1, "worker-a", now=5)
    assert _texts(claim) == ["Once resumed, use the cached export"]


def test_terminal_job_rejects_operator_steer(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    claim = store.claim_turn("job_1", 1, "worker-a", now=2)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "CANDIDATE_DONE", "complete", now=3,
    )
    store.verify_candidate("job_1", True, "verified", now=4)

    with pytest.raises(JobStoreError) as exc:
        store.queue_operator_steer("job_1", "too late", now=5)
    assert exc.value.code == "JOB_TERMINAL"


def test_pending_steer_supersedes_candidate_done_before_verification_commits(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(plan=["Produce final answer"]), now=1)
    claim = store.claim_turn("job_1", 1, "worker-a", now=2)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "CANDIDATE_DONE", "candidate complete", now=3,
    )
    assert store.get_job_status("job_1")["status"] == "VERIFYING"

    store.queue_operator_steer("job_1", "Also include the sensitivity table", now=4)
    verified = store.verify_candidate("job_1", True, "checks passed", now=5)

    assert verified["status"] == "READY"
    assert verified["completion_deferred"] is True
    assert verified["next_seq"] == 2
    followup = store.claim_turn("job_1", 2, "worker-b", now=6)
    assert followup["turn_total"] is None
    assert _texts(followup) == ["Also include the sensitivity table"]


def test_failed_verification_and_pending_steer_share_the_next_turn(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    claim = store.claim_turn("job_1", 1, "worker-a", now=2)
    store.commit_turn(
        "job_1", 1, claim["lease_id"], claim["fencing_token"],
        "CANDIDATE_DONE", "candidate complete", now=3,
    )
    store.queue_operator_steer("job_1", "Keep the chart but change the denominator", now=4)

    failed = store.verify_candidate("job_1", False, "one check failed", now=5)
    assert failed["status"] == "READY"
    followup = store.claim_turn("job_1", failed["next_seq"], "worker-b", now=6)
    assert "one check failed" in followup["instruction"]
    assert _texts(followup) == ["Keep the chart but change the denominator"]


def test_console_projection_surfaces_pending_steer_as_real_progress(tmp_path):
    store = _store(tmp_path)
    store.create_job(_job(), now=1)
    store.queue_operator_steer("job_1", "Prioritize the executive summary", now=2)

    worker = store.console_snapshot()["workers"][0]
    assert worker["execution"]["operator_steer_pending"] == 1
    assert worker["execution"]["last_progress"] == "Prioritize the executive summary"
