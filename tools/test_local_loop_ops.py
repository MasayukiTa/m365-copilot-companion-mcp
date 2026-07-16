from types import SimpleNamespace

from tools import local_loop_ops as ops


def test_mutating_job_tools_fail_closed_when_not_unlocked(monkeypatch):
    monkeypatch.setattr(ops, "require_unlocked", lambda: "locked")
    result = ops.claim_turn("job", 1, "worker")
    assert result == {"ok": False, "error": "LOCKED", "detail": "locked"}


def test_status_is_read_only_and_store_errors_are_structured(monkeypatch):
    class FakeStore:
        def get_job_status(self, job_id):
            raise ops.JobStoreError("JOB_NOT_FOUND", "missing")

    monkeypatch.setattr(ops, "_store", lambda: FakeStore())
    assert ops.get_job_status("missing") == {
        "ok": False, "error": "JOB_NOT_FOUND", "detail": "missing",
    }


def test_claim_default_lease_covers_long_review_turn(monkeypatch):
    seen = {}

    class FakeStore:
        def claim_turn(self, *args):
            seen["args"] = args
            return {"ok": True}

    monkeypatch.setattr(ops, "require_unlocked", lambda: None)
    monkeypatch.setattr(ops, "_store", lambda: FakeStore())
    assert ops.claim_turn("j", 3, "worker")["ok"]
    assert seen["args"] == ("j", 3, "worker", 3600)


def test_commit_forwards_fencing_token(monkeypatch):
    seen = {}

    class FakeStore:
        def commit_turn(self, *args):
            seen["args"] = args
            return {"ok": True}

    monkeypatch.setattr(ops, "require_unlocked", lambda: None)
    monkeypatch.setattr(ops, "_store", lambda: FakeStore())
    assert ops.commit_turn("j", 2, "lease", 7, "CONTINUE", "s", "next")["ok"]
    assert seen["args"][:4] == ("j", 2, "lease", 7)
