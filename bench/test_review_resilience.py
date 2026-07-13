from relay.review_resilience import (
    RecoveryAction,
    RefusalCause,
    TaskEnvelope,
    diagnose_after_fresh_replay,
    looks_like_policy_refusal,
    same_task_envelope,
)
from relay.relay_fleet import RelayWorker


def _env(**overrides):
    data = dict(
        task_id="review-1",
        parent_task_id=None,
        campaign_id="campaign-1",
        role="producer",
        goal_text="authorized review",
        cwd="C:/repo",
        metadata={
            "scope": ["a.py"],
            "output_contract": "FINDINGS",
            "authorization_preamble": "authorized",
        },
    )
    data.update(overrides)
    return TaskEnvelope(**data)


def test_policy_refusal_detection_is_specific():
    assert looks_like_policy_refusal("このリクエストには対応できません")
    assert looks_like_policy_refusal("I cannot assist with that request.")
    assert not looks_like_policy_refusal("Network error. Please try again later.")
    assert not looks_like_policy_refusal("No tools are assigned to this session.")
    assert not looks_like_policy_refusal("")


def test_task_envelope_hash_ignores_only_session_attempt():
    a = _env(session_attempt=0)
    b = _env(session_attempt=1)
    assert a.goal_hash == b.goal_hash
    assert same_task_envelope(a, b)
    c = _env(metadata={**a.metadata, "scope": ["b.py"]})
    assert c.goal_hash != a.goal_hash


def test_diagnosis_after_fresh_replay():
    recovered = diagnose_after_fresh_replay(True, False, True, False)
    assert recovered.cause == RefusalCause.SESSION_STATE
    assert recovered.action == RecoveryAction.FRESH_REPLAY

    refused = diagnose_after_fresh_replay(True, True, False, False)
    assert refused.cause == RefusalCause.TASK_CONTENT
    assert refused.action == RecoveryAction.DECOMPOSE


def test_worker_policy_refusal_replays_once_then_marks_content_refused(monkeypatch):
    goal = {
        "text": "authorized review",
        "cwd": "C:/repo",
        "task_id": "review-1",
        "campaign_id": "campaign-1",
        "role": "producer",
        "metadata": {"scope": ["a.py"], "output_contract": "FINDINGS"},
    }
    worker = RelayWorker(goal, "w0", resilience_profile="review", max_fresh_replays=1)
    called = []

    def fake_replay():
        called.append(True)
        worker.fresh_replay_count = 1
        return True

    monkeypatch.setattr(worker, "_start_fresh_replay", fake_replay)
    worker._decide("このリクエストには対応できません")
    assert called == [True]
    assert worker.outcome is None

    worker._decide("I cannot assist with that request")
    assert worker.status == "content_refused"
    assert worker.outcome == "CONTENT_REFUSED"
    assert worker.recovery_result == "needs_decomposition"


def test_worker_profile_off_keeps_legacy_decision_path(monkeypatch):
    worker = RelayWorker("ordinary goal", "w0", resilience_profile="off",
                         max_fresh_replays=0, max_no_progress=99)
    monkeypatch.setattr(worker, "_start_fresh_replay", lambda: (_ for _ in ()).throw(
        AssertionError("must not replay")))
    worker._decide("I cannot assist with that request")
    assert worker.status == "ready"
    assert worker.outcome is None


def test_resilient_transcript_is_attempt_scoped_and_replay_is_hard_capped(tmp_path):
    worker = RelayWorker(
        {"text": "goal", "task_id": "t", "campaign_id": "c"}, "w0",
        transcript_dir=str(tmp_path), run_id="run", resilience_profile="review",
        max_fresh_replays=99,
    )
    assert worker.transcript.endswith("run_w0_a0.jsonl")
    assert worker.attempt_transcripts == [worker.transcript]
    assert worker.max_fresh_replays == 1
