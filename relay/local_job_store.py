"""SQLite-backed control plane for response-content-independent Copilot turns.

The database is local to one PC and requires no service or administrator rights.
Every state transition that can race uses ``BEGIN IMMEDIATE`` and every lease is
fenced, so an expired browser turn cannot commit after a replacement claimed it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from relay.execution_profiles import ExecutionProfile, resolve_profile


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / ".jobs" / "jobs.sqlite3"
TERMINAL_JOB_STATUSES = frozenset({"DONE", "FAILED", "CANCELLED"})
INTERACTION_WAIT_STATUSES = frozenset({"WAITING_AUTH", "WAITING_CONSENT"})
COMMIT_STATUSES = frozenset({
    "CONTINUE", "CANDIDATE_DONE", "WAITING_USER", "WAITING_EXTERNAL", "NEEDS_ROUTING",
})
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class JobStoreError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "detail": str(self)}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value, max_bytes: int, field: str) -> str:
    text = str(value or "")
    if len(text.encode("utf-8")) > max_bytes:
        raise JobStoreError("PAYLOAD_TOO_LARGE", f"{field} exceeds {max_bytes} UTF-8 bytes")
    return text


class LocalJobStore:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("MCP_LOCAL_JOB_DB") or DEFAULT_DB_PATH).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    execution_profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_seq INTEGER NOT NULL,
                    last_committed_seq INTEGER NOT NULL DEFAULT 0,
                    job_json TEXT NOT NULL,
                    verification_detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    instruction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    lease_id TEXT,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at REAL,
                    commit_hash TEXT,
                    commit_json TEXT,
                    abort_hash TEXT,
                    abort_json TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (job_id, seq),
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    seq INTEGER,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_job_id_idx ON events(job_id, id);
                """
            )
        finally:
            conn.close()

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        value = str(job_id or "")
        if not JOB_ID_RE.fullmatch(value):
            raise JobStoreError("INVALID_JOB_ID", "job_id must be 1-128 safe ASCII characters")
        return value

    @staticmethod
    def _event(conn, job_id: str, seq: int | None, event_type: str, payload: dict, now: float):
        conn.execute(
            "INSERT INTO events(job_id,seq,event_type,payload_json,created_at) VALUES(?,?,?,?,?)",
            (job_id, seq, event_type, _json(payload), now),
        )

    def create_job(self, job: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        data = dict(job or {})
        job_id = self._validate_job_id(data.get("job_id"))
        try:
            profile = resolve_profile(data)
        except Exception as exc:
            raise JobStoreError("INVALID_PROFILE", str(exc)) from exc
        if profile != ExecutionProfile.LOCAL_LOOP:
            raise JobStoreError("PROFILE_MISMATCH", "local store accepts LOCAL_LOOP jobs only")
        data["execution_profile"] = profile.value
        seq = int(data.get("current_seq", 1))
        if seq < 1:
            raise JobStoreError("INVALID_SEQ", "current_seq must be >= 1")
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
        max_claim = int(constraints.get("max_claim_bytes", 8192))
        instruction = _bounded_text(task.get("instruction", ""), max_claim, "instruction").strip()
        if not instruction:
            raise JobStoreError("INVALID_INSTRUCTION", "task.instruction is required")
        data["status"] = "READY"
        data["current_seq"] = seq
        payload = _json(data)
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
                raise JobStoreError("JOB_EXISTS", f"job {job_id!r} already exists")
            conn.execute(
                "INSERT INTO jobs(job_id,execution_profile,status,current_seq,last_committed_seq,"
                "job_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, profile.value, "READY", seq, seq - 1, payload, now, now),
            )
            conn.execute(
                "INSERT INTO turns(job_id,seq,instruction,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, seq, instruction, "READY", now, now),
            )
            self._event(conn, job_id, seq, "JOB_CREATED", {"profile": profile.value}, now)
        return self.get_job_status(job_id)

    def _job_and_turn(self, conn, job_id: str, seq: int | None = None):
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            raise JobStoreError("JOB_NOT_FOUND", f"job {job_id!r} not found")
        target = int(job["current_seq"] if seq is None else seq)
        turn = conn.execute(
            "SELECT * FROM turns WHERE job_id=? AND seq=?", (job_id, target)
        ).fetchone()
        if not turn:
            raise JobStoreError("TURN_NOT_FOUND", f"turn {job_id!r}/{target} not found")
        return job, turn

    def claim_turn(self, job_id: str, expected_seq: int, worker_id: str,
                   lease_seconds: int = 300, now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        worker_id = _bounded_text(worker_id, 256, "worker_id").strip()
        if not worker_id:
            raise JobStoreError("INVALID_WORKER", "worker_id is required")
        lease_seconds = max(30, min(int(lease_seconds), 3600))
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["execution_profile"] != ExecutionProfile.LOCAL_LOOP.value:
                raise JobStoreError("PROFILE_MISMATCH", "claim_turn accepts LOCAL_LOOP jobs only")
            if job["status"] in TERMINAL_JOB_STATUSES | INTERACTION_WAIT_STATUSES | {
                "VERIFYING", "WAITING_USER", "WAITING_EXTERNAL", "NEEDS_ROUTING",
                "WAITING_RUNTIME",
            }:
                raise JobStoreError("JOB_NOT_CLAIMABLE", f"job status is {job['status']}")
            if int(expected_seq) != int(job["current_seq"]):
                raise JobStoreError(
                    "SEQ_MISMATCH", f"expected {expected_seq}, current seq is {job['current_seq']}",
                )
            expires = float(turn["lease_expires_at"] or 0)
            if turn["lease_id"] and expires > now:
                if turn["worker_id"] == worker_id:
                    return self._claim_result(job, turn)
                raise JobStoreError("LEASE_ACTIVE", "turn already has an active lease")
            fence = int(turn["fencing_token"] or 0) + 1
            lease_id = "lease_" + secrets.token_urlsafe(24)
            lease_expires = now + lease_seconds
            conn.execute(
                "UPDATE turns SET status='LEASED',worker_id=?,lease_id=?,fencing_token=?,"
                "lease_expires_at=?,abort_hash=NULL,abort_json=NULL,updated_at=? "
                "WHERE job_id=? AND seq=?",
                (worker_id, lease_id, fence, lease_expires, now, job_id, expected_seq),
            )
            conn.execute(
                "UPDATE jobs SET status='RUNNING',updated_at=? WHERE job_id=?", (now, job_id),
            )
            self._event(conn, job_id, int(expected_seq), "TURN_CLAIMED", {
                "worker_id": worker_id, "fencing_token": fence,
                "lease_expires_at": lease_expires,
            }, now)
            job, turn = self._job_and_turn(conn, job_id)
            return self._claim_result(job, turn)

    def _claim_result(self, job, turn) -> dict:
        data = json.loads(job["job_json"])
        constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
        previous_summary = ""
        # Read-only helper connection is safe here; _claim_result is also used during a tx.
        conn = self._connect()
        try:
            prev = conn.execute(
                "SELECT commit_json FROM turns WHERE job_id=? AND seq<? AND commit_json IS NOT NULL "
                "ORDER BY seq DESC LIMIT 1", (job["job_id"], turn["seq"]),
            ).fetchone()
            if prev:
                previous_summary = str(json.loads(prev["commit_json"]).get("summary", ""))
        finally:
            conn.close()
        previous_summary = previous_summary.encode("utf-8")[:2048].decode("utf-8", "ignore")
        return {
            "ok": True,
            "job_id": job["job_id"],
            "seq": int(turn["seq"]),
            "lease_id": turn["lease_id"],
            "fencing_token": int(turn["fencing_token"]),
            "lease_expires_at": float(turn["lease_expires_at"]),
            "instruction": turn["instruction"],
            "context": {
                "workspace": constraints.get("allowed_base") or data.get("workspace") or "",
                "previous_summary": previous_summary,
                "constraints": constraints,
            },
        }

    def _validate_lease(self, turn, lease_id: str, fencing_token: int, now: float):
        if not turn["lease_id"] or not secrets.compare_digest(str(turn["lease_id"]), str(lease_id)):
            raise JobStoreError("LEASE_MISMATCH", "lease_id is not current")
        if int(turn["fencing_token"]) != int(fencing_token):
            raise JobStoreError("FENCE_MISMATCH", "fencing token is stale")
        if float(turn["lease_expires_at"] or 0) <= now:
            raise JobStoreError("LEASE_EXPIRED", "lease has expired")

    def heartbeat(self, job_id: str, seq: int, lease_id: str, fencing_token: int,
                  phase: str, detail: str = "", extend_seconds: int = 300,
                  now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        phase = _bounded_text(phase, 256, "phase")
        detail = _bounded_text(detail, 2048, "detail")
        extend_seconds = max(30, min(int(extend_seconds), 3600))
        with self._transaction() as conn:
            _, turn = self._job_and_turn(conn, job_id, int(seq))
            self._validate_lease(turn, lease_id, fencing_token, now)
            expires = now + extend_seconds
            conn.execute(
                "UPDATE turns SET lease_expires_at=?,updated_at=? WHERE job_id=? AND seq=?",
                (expires, now, job_id, int(seq)),
            )
            self._event(conn, job_id, int(seq), "HEARTBEAT", {
                "phase": phase, "detail": detail, "lease_expires_at": expires,
            }, now)
        return {"ok": True, "phase": phase, "lease_expires_at": expires}

    def commit_turn(self, job_id: str, seq: int, lease_id: str, fencing_token: int,
                    status: str, summary: str, next_instruction: str = "",
                    artifacts: list[dict] | None = None, metrics: dict | None = None,
                    now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        status = str(status or "").upper()
        if status not in COMMIT_STATUSES:
            raise JobStoreError("INVALID_COMMIT_STATUS", f"unsupported status {status!r}")
        artifacts = list(artifacts or [])
        metrics = dict(metrics or {})
        if len(artifacts) > 64:
            raise JobStoreError("PAYLOAD_TOO_LARGE", "artifacts exceeds 64 entries")
        if len(_json(artifacts).encode("utf-8")) > 65536 or len(_json(metrics).encode("utf-8")) > 16384:
            raise JobStoreError("PAYLOAD_TOO_LARGE", "artifacts or metrics payload is too large")
        payload = {
            "job_id": job_id, "seq": int(seq), "status": status,
            "summary": str(summary or ""), "next_instruction": str(next_instruction or ""),
            "artifacts": artifacts, "metrics": metrics,
        }
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id, int(seq))
            data = json.loads(job["job_json"])
            constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
            payload["summary"] = _bounded_text(
                payload["summary"], int(constraints.get("max_commit_summary_bytes", 4096)), "summary",
            )
            payload["next_instruction"] = _bounded_text(
                payload["next_instruction"], int(constraints.get("max_claim_bytes", 8192)),
                "next_instruction",
            )
            digest = _hash(payload)
            if turn["commit_hash"]:
                if secrets.compare_digest(str(turn["commit_hash"]), digest):
                    return {
                        "ok": True, "idempotent": True, "committed_seq": int(seq),
                        "next_seq": int(job["current_seq"]),
                        "ack": f"ACK {job_id} seq={seq}",
                    }
                raise JobStoreError("COMMIT_CONFLICT", "turn already has a different commit")
            self._validate_lease(turn, lease_id, fencing_token, now)
            if int(job["current_seq"]) != int(seq):
                raise JobStoreError("SEQ_MISMATCH", "job advanced before commit")
            if status == "CONTINUE" and not payload["next_instruction"].strip():
                raise JobStoreError("NEXT_INSTRUCTION_REQUIRED", "CONTINUE requires next_instruction")
            stored = dict(payload, committed_at=now, commit_hash=digest)
            conn.execute(
                "UPDATE turns SET status='COMMITTED',commit_hash=?,commit_json=?,updated_at=? "
                "WHERE job_id=? AND seq=?",
                (digest, _json(stored), now, job_id, int(seq)),
            )
            next_seq = int(seq)
            if status == "CONTINUE":
                next_seq = int(seq) + 1
                conn.execute(
                    "INSERT INTO turns(job_id,seq,instruction,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (job_id, next_seq, payload["next_instruction"], "READY", now, now),
                )
                job_status = "READY"
            elif status == "CANDIDATE_DONE":
                job_status = "VERIFYING"
            else:
                job_status = status
            conn.execute(
                "UPDATE jobs SET status=?,current_seq=?,last_committed_seq=?,updated_at=? "
                "WHERE job_id=?",
                (job_status, next_seq, int(seq), now, job_id),
            )
            self._event(conn, job_id, int(seq), "TURN_COMMITTED", {
                "status": status, "summary": payload["summary"], "artifacts": artifacts,
            }, now)
        return {
            "ok": True, "idempotent": False, "committed_seq": int(seq),
            "next_seq": next_seq, "ack": f"ACK {job_id} seq={seq}",
        }

    def abort_turn(self, job_id: str, seq: int, lease_id: str, fencing_token: int,
                   error_code: str, detail: str, retryable: bool,
                   now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        payload = {
            "job_id": job_id, "seq": int(seq),
            "error_code": _bounded_text(error_code, 256, "error_code"),
            "detail": _bounded_text(detail, 4096, "detail"),
            "retryable": bool(retryable),
        }
        digest = _hash(payload)
        with self._transaction() as conn:
            _, turn = self._job_and_turn(conn, job_id, int(seq))
            if turn["abort_hash"]:
                if secrets.compare_digest(str(turn["abort_hash"]), digest):
                    return {"ok": True, "idempotent": True, "retryable": bool(retryable)}
                raise JobStoreError("ABORT_CONFLICT", "turn already has a different abort")
            self._validate_lease(turn, lease_id, fencing_token, now)
            stored = dict(payload, aborted_at=now, abort_hash=digest)
            if retryable:
                conn.execute(
                    "UPDATE turns SET status='READY',worker_id=NULL,lease_id=NULL,"
                    "lease_expires_at=NULL,abort_hash=?,abort_json=?,retry_count=retry_count+1,"
                    "updated_at=? WHERE job_id=? AND seq=?",
                    (digest, _json(stored), now, job_id, int(seq)),
                )
                job_status = "READY"
            else:
                conn.execute(
                    "UPDATE turns SET status='ABORTED',abort_hash=?,abort_json=?,updated_at=? "
                    "WHERE job_id=? AND seq=?",
                    (digest, _json(stored), now, job_id, int(seq)),
                )
                job_status = "FAILED"
            conn.execute("UPDATE jobs SET status=?,updated_at=? WHERE job_id=?", (job_status, now, job_id))
            self._event(conn, job_id, int(seq), "TURN_ABORTED", payload, now)
        return {"ok": True, "idempotent": False, "retryable": bool(retryable)}

    def verify_candidate(self, job_id: str, passed: bool, detail: str = "",
                         failure_instruction: str = "", now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        detail = _bounded_text(detail, 4096, "verification detail")
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] != "VERIFYING":
                if bool(passed) and job["status"] == "DONE":
                    return {"ok": True, "idempotent": True, "status": "DONE"}
                raise JobStoreError("NOT_VERIFYING", f"job status is {job['status']}")
            commit = json.loads(turn["commit_json"] or "{}")
            if commit.get("status") != "CANDIDATE_DONE":
                raise JobStoreError("NO_CANDIDATE", "current turn is not CANDIDATE_DONE")
            if passed:
                conn.execute(
                    "UPDATE jobs SET status='DONE',verification_detail=?,updated_at=? WHERE job_id=?",
                    (detail, now, job_id),
                )
                self._event(conn, job_id, int(turn["seq"]), "VERIFICATION_PASSED", {"detail": detail}, now)
                return {"ok": True, "idempotent": False, "status": "DONE"}
            data = json.loads(job["job_json"])
            constraints = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
            instruction = failure_instruction or (
                "Local acceptance checks failed. Fix the failure and re-run the checks.\n" + detail
            )
            instruction = _bounded_text(
                instruction, int(constraints.get("max_claim_bytes", 8192)), "failure_instruction",
            )
            next_seq = int(turn["seq"]) + 1
            conn.execute(
                "INSERT INTO turns(job_id,seq,instruction,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, next_seq, instruction, "READY", now, now),
            )
            conn.execute(
                "UPDATE jobs SET status='READY',current_seq=?,verification_detail=?,updated_at=? "
                "WHERE job_id=?", (next_seq, detail, now, job_id),
            )
            self._event(conn, job_id, next_seq, "VERIFICATION_FAILED", {"detail": detail}, now)
        return {"ok": True, "idempotent": False, "status": "READY", "next_seq": next_seq}

    def read_job_context(self, job_id: str, seq: int, lease_id: str, fencing_token: int,
                         keys: list[str] | None = None, now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        keys = list(keys or [])[:16]
        allowed = {"task", "constraints", "acceptance_checks", "data_location", "requires_local_tool"}
        if any(k not in allowed for k in keys):
            raise JobStoreError("INVALID_CONTEXT_KEY", "one or more context keys are not allowed")
        conn = self._connect()
        try:
            job, turn = self._job_and_turn(conn, job_id, int(seq))
            self._validate_lease(turn, lease_id, fencing_token, now)
            data = json.loads(job["job_json"])
            return {"ok": True, "job_id": job_id, "seq": int(seq),
                    "context": {k: data.get(k) for k in keys}}
        finally:
            conn.close()

    def get_turn_commit(self, job_id: str, seq: int) -> dict | None:
        job_id = self._validate_job_id(job_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT commit_json FROM turns WHERE job_id=? AND seq=?", (job_id, int(seq)),
            ).fetchone()
            if not row:
                raise JobStoreError("TURN_NOT_FOUND", f"turn {job_id!r}/{seq} not found")
            return json.loads(row["commit_json"]) if row["commit_json"] else None
        finally:
            conn.close()

    def get_job(self, job_id: str) -> dict:
        job_id = self._validate_job_id(job_id)
        conn = self._connect()
        try:
            row = conn.execute("SELECT job_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobStoreError("JOB_NOT_FOUND", f"job {job_id!r} not found")
            return json.loads(row["job_json"])
        finally:
            conn.close()

    def get_job_status(self, job_id: str, event_limit: int = 8) -> dict:
        job_id = self._validate_job_id(job_id)
        conn = self._connect()
        try:
            job, turn = self._job_and_turn(conn, job_id)
            events = conn.execute(
                "SELECT seq,event_type,payload_json,created_at FROM events WHERE job_id=? "
                "ORDER BY id DESC LIMIT ?", (job_id, max(0, min(int(event_limit), 50))),
            ).fetchall()
            commit = json.loads(turn["commit_json"]) if turn["commit_json"] else None
            if commit is None:
                previous = conn.execute(
                    "SELECT commit_json FROM turns WHERE job_id=? AND commit_json IS NOT NULL "
                    "ORDER BY seq DESC LIMIT 1", (job_id,),
                ).fetchone()
                if previous:
                    commit = json.loads(previous["commit_json"])
            return {
                "ok": True, "job_id": job_id, "execution_profile": job["execution_profile"],
                "status": job["status"], "current_seq": int(job["current_seq"]),
                "last_committed_seq": int(job["last_committed_seq"]),
                "turn_status": turn["status"], "lease_expires_at": turn["lease_expires_at"],
                "retry_count": int(turn["retry_count"]), "commit": commit,
                "verification_detail": job["verification_detail"],
                "events": [{"seq": e["seq"], "event": e["event_type"],
                            "payload": json.loads(e["payload_json"]), "ts": e["created_at"]}
                           for e in reversed(events)],
                "updated_at": float(job["updated_at"]),
            }
        finally:
            conn.close()

    def list_job_statuses(self) -> list[dict]:
        conn = self._connect()
        try:
            ids = [row[0] for row in conn.execute("SELECT job_id FROM jobs ORDER BY created_at")]
        finally:
            conn.close()
        return [self.get_job_status(job_id) for job_id in ids]

    def cancel_job(self, job_id: str, reason: str = "operator stop",
                   now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        reason = _bounded_text(reason, 1024, "cancel reason")
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] == "CANCELLED":
                return {"ok": True, "idempotent": True, "status": "CANCELLED"}
            if job["status"] in {"DONE", "FAILED"}:
                raise JobStoreError("JOB_TERMINAL", f"job status is {job['status']}")
            conn.execute(
                "UPDATE jobs SET status='CANCELLED',verification_detail=?,updated_at=? "
                "WHERE job_id=?", (reason, now, job_id),
            )
            conn.execute(
                "UPDATE turns SET lease_expires_at=? WHERE job_id=? AND seq=?",
                (now, job_id, int(turn["seq"])),
            )
            self._event(conn, job_id, int(turn["seq"]), "JOB_CANCELLED", {"reason": reason}, now)
        return {"ok": True, "idempotent": False, "status": "CANCELLED"}

    def record_event(self, job_id: str, event_type: str, payload: dict | None = None,
                     seq: int | None = None, now: float | None = None) -> None:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        with self._transaction() as conn:
            job = conn.execute("SELECT current_seq FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise JobStoreError("JOB_NOT_FOUND", f"job {job_id!r} not found")
            self._event(
                conn, job_id, int(job["current_seq"] if seq is None else seq),
                _bounded_text(event_type, 128, "event_type"), dict(payload or {}), now,
            )

    def mark_waiting_runtime(self, job_id: str, reason: str,
                             now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        reason = _bounded_text(reason, 2048, "runtime reason")
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise JobStoreError("JOB_TERMINAL", f"job status is {job['status']}")
            conn.execute(
                "UPDATE jobs SET status='WAITING_RUNTIME',verification_detail=?,updated_at=? "
                "WHERE job_id=?", (reason, now, job_id),
            )
            self._event(conn, job_id, int(turn["seq"]), "WAITING_RUNTIME", {"reason": reason}, now)
        return {"ok": True, "status": "WAITING_RUNTIME"}

    def mark_waiting_interaction(self, job_id: str, status: str, reason: str,
                                 now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        status = str(status or "").upper()
        if status not in INTERACTION_WAIT_STATUSES:
            raise JobStoreError("INVALID_WAIT_STATUS", f"unsupported wait status {status!r}")
        reason = _bounded_text(reason, 2048, "interaction reason")
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise JobStoreError("JOB_TERMINAL", f"job status is {job['status']}")
            if job["status"] == status:
                return {"ok": True, "idempotent": True, "status": status}
            conn.execute(
                "UPDATE jobs SET status=?,verification_detail=?,updated_at=? WHERE job_id=?",
                (status, reason, now, job_id),
            )
            self._event(conn, job_id, int(turn["seq"]), status, {"reason": reason}, now)
        return {"ok": True, "idempotent": False, "status": status}

    def resume_interaction(self, job_id: str, now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] not in INTERACTION_WAIT_STATUSES:
                return {"ok": True, "idempotent": True, "status": job["status"]}
            prior = job["status"]
            # Expire the prompt-bound lease so the resumed RUN gets a higher fencing token.
            conn.execute(
                "UPDATE turns SET lease_expires_at=? WHERE job_id=? AND seq=?",
                (now, job_id, int(turn["seq"])),
            )
            conn.execute(
                "UPDATE jobs SET status='READY',verification_detail='',updated_at=? WHERE job_id=?",
                (now, job_id),
            )
            self._event(conn, job_id, int(turn["seq"]), "INTERACTION_RESUMED", {
                "prior_status": prior,
            }, now)
        return {"ok": True, "idempotent": False, "status": "READY"}

    def resume_runtime(self, job_id: str, now: float | None = None) -> dict:
        job_id = self._validate_job_id(job_id)
        now = time.time() if now is None else float(now)
        with self._transaction() as conn:
            job, turn = self._job_and_turn(conn, job_id)
            if job["status"] != "WAITING_RUNTIME":
                return {"ok": True, "idempotent": True, "status": job["status"]}
            # A lease may still belong to a browser turn that vanished. Expire it now; the
            # next claim increments the fencing token and makes any late commit harmless.
            conn.execute(
                "UPDATE turns SET lease_expires_at=? WHERE job_id=? AND seq=?",
                (now, job_id, int(turn["seq"])),
            )
            conn.execute(
                "UPDATE jobs SET status='READY',verification_detail='',updated_at=? WHERE job_id=?",
                (now, job_id),
            )
            self._event(conn, job_id, int(turn["seq"]), "RUNTIME_RESUMED", {}, now)
        return {"ok": True, "idempotent": False, "status": "READY"}

    def console_snapshot(self) -> dict:
        """Project SQLite state into the FleetCockpit-compatible status shape."""
        statuses = self.list_job_statuses()
        workers = []
        done = 0
        for item in statuses:
            commit = item.get("commit") or {}
            terminal = item["status"] in TERMINAL_JOB_STATUSES
            if item["status"] == "DONE":
                done += 1
            workers.append({
                "name": item["job_id"], "goal": self.get_job(item["job_id"]).get("task", {}).get("instruction", ""),
                "status": item["status"].lower(), "outcome": item["status"] if terminal else None,
                "turn": item["current_seq"], "reason": item.get("verification_detail", ""),
                "last": commit.get("summary", ""), "transcript": "", "closed": terminal,
                "execution_profile": item["execution_profile"],
                "artifacts": commit.get("artifacts", []), "phase_events": item.get("events", []),
            })
        now = time.time()
        return {
            "started": min((s["updated_at"] for s in statuses), default=now),
            "updated": now, "total": len(statuses), "done_count": done,
            "running": any(not w["closed"] for w in workers), "open_tabs": 0,
            "execution_mode": "LOCAL_LOOP", "workers": workers,
        }

    def checkpoint(self) -> dict:
        """Bound WAL growth without a server or VACUUM during active work."""
        conn = self._connect()
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return {"ok": True, "busy": int(row[0]), "log_frames": int(row[1]),
                    "checkpointed_frames": int(row[2])}
        finally:
            conn.close()
