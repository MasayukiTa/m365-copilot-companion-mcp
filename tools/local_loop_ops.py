"""MCP tools for the SQLite-backed LOCAL_LOOP control plane."""

from __future__ import annotations

import os
from pathlib import Path

from relay.local_job_store import DEFAULT_DB_PATH, JobStoreError, LocalJobStore
from tools.security import require_unlocked


_STORE = None
_STORE_PATH = None


def _store() -> LocalJobStore:
    global _STORE, _STORE_PATH
    path = str(Path(os.environ.get("MCP_LOCAL_JOB_DB") or DEFAULT_DB_PATH).resolve())
    if _STORE is None or _STORE_PATH != path:
        _STORE = LocalJobStore(path)
        _STORE_PATH = path
    return _STORE


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except JobStoreError as exc:
        return exc.as_dict()
    except Exception as exc:
        return {"ok": False, "error": "STORE_ERROR", "detail": f"{type(exc).__name__}: {exc}"}


def claim_turn(job_id: str, expected_seq: int, worker_id: str,
               lease_seconds: int = 300) -> dict:
    """Claim one LOCAL_LOOP turn. Call this first after RUN; requires unlock."""
    error = require_unlocked()
    if error:
        return {"ok": False, "error": "LOCKED", "detail": error}
    return _call(_store().claim_turn, job_id, expected_seq, worker_id, lease_seconds)


def heartbeat(job_id: str, seq: int, lease_id: str, fencing_token: int,
              phase: str, detail: str = "", extend_seconds: int = 300) -> dict:
    """Extend the current fenced lease while a LOCAL_LOOP turn is still working."""
    error = require_unlocked()
    if error:
        return {"ok": False, "error": "LOCKED", "detail": error}
    return _call(
        _store().heartbeat, job_id, seq, lease_id, fencing_token,
        phase, detail, extend_seconds,
    )


def commit_turn(job_id: str, seq: int, lease_id: str, fencing_token: int,
                status: str, summary: str, next_instruction: str = "",
                artifacts: list[dict] | None = None, metrics: dict | None = None) -> dict:
    """Commit structured turn state. Use CANDIDATE_DONE, never authoritative DONE."""
    error = require_unlocked()
    if error:
        return {"ok": False, "error": "LOCKED", "detail": error}
    return _call(
        _store().commit_turn, job_id, seq, lease_id, fencing_token,
        status, summary, next_instruction, artifacts, metrics,
    )


def abort_turn(job_id: str, seq: int, lease_id: str, fencing_token: int,
               error_code: str, detail: str, retryable: bool) -> dict:
    """Abort a LOCAL_LOOP turn, preserving retryability and its fenced lease."""
    error = require_unlocked()
    if error:
        return {"ok": False, "error": "LOCKED", "detail": error}
    return _call(
        _store().abort_turn, job_id, seq, lease_id, fencing_token,
        error_code, detail, retryable,
    )


def read_job_context(job_id: str, seq: int, lease_id: str, fencing_token: int,
                     keys: list[str]) -> dict:
    """Read bounded job context after a successful claim; the current lease is required."""
    return _call(
        _store().read_job_context, job_id, seq, lease_id, fencing_token, keys,
    )


def get_job_status(job_id: str) -> dict:
    """Read LOCAL_LOOP status, latest structured commit, events and artifact references."""
    return _call(_store().get_job_status, job_id)
