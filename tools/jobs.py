import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .file_ops import _validate_path
from .security import require_unlocked


class _Job:
    __slots__ = (
        "id",
        "label",
        "kind",
        "command",
        "process",
        "started_at",
        "finished_at",
        "returncode",
        "stdout_path",
        "stderr_path",
    )

    def __init__(self, kind: str, label: str, command: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label
        self.command = command
        self.process: Optional[subprocess.Popen] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.stdout_path: Optional[str] = None
        self.stderr_path: Optional[str] = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def refresh(self) -> None:
        if self.process is None or self.finished_at is not None:
            return
        rc = self.process.poll()
        if rc is not None:
            self.returncode = rc
            self.finished_at = time.time()


_JOBS: dict[str, _Job] = {}
_LOCK = threading.Lock()


def _create_log_paths() -> tuple[str, str]:
    base = Path(tempfile.gettempdir()) / "m365-copilot-companion-mcp-jobs"
    base.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex[:10]
    return str(base / f"{stem}.out.log"), str(base / f"{stem}.err.log")


def run_in_background(
    command: str,
    label: str = "",
    working_dir: Optional[str] = None,
) -> str:
    """Start a shell command in the background and return a job_id immediately.

    Use job_status / job_wait / job_output / job_list / job_kill to manage it.

    Args:
        command: Shell command to execute.
        label: Short human label so you can find this job later.
        working_dir: Optional working directory under the allowed base.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        cwd = os.getcwd()
        if working_dir:
            p = _validate_path(working_dir)
            if not p.is_dir():
                return f"[run_in_background error: not a directory: {p}]"
            cwd = str(p)
        out_path, err_path = _create_log_paths()
        out_f = open(out_path, "w", encoding="utf-8")
        err_f = open(err_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=out_f,
            stderr=err_f,
            cwd=cwd,
        )
        job = _Job("shell", label or command[:60], command)
        job.process = proc
        job.stdout_path = out_path
        job.stderr_path = err_path
        with _LOCK:
            _JOBS[job.id] = job
        return f"job_id: {job.id}\nlabel: {job.label}\npid: {proc.pid}\nstarted_at: {job.started_at:.0f}"
    except Exception as e:
        return f"[run_in_background error: {type(e).__name__}: {e}]"


def run_python_in_background(code: str, label: str = "") -> str:
    """Start a Python script in the background and return a job_id immediately.

    Args:
        code: Python source code to execute.
        label: Short human label.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            script_path = f.name
        out_path, err_path = _create_log_paths()
        out_f = open(out_path, "w", encoding="utf-8")
        err_f = open(err_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, script_path],
            stdout=out_f,
            stderr=err_f,
        )
        job = _Job("python", label or "python script", script_path)
        job.process = proc
        job.stdout_path = out_path
        job.stderr_path = err_path
        with _LOCK:
            _JOBS[job.id] = job
        return f"job_id: {job.id}\nlabel: {job.label}\npid: {proc.pid}"
    except Exception as e:
        return f"[run_python_in_background error: {type(e).__name__}: {e}]"


def job_status(job_id: str) -> str:
    """Return the current status of a background job."""
    job = _JOBS.get(job_id)
    if job is None:
        return f"[job_status error: unknown job_id {job_id!r}]"
    job.refresh()
    state = "running" if job.is_running() else "finished"
    parts = [f"id: {job.id}", f"label: {job.label}", f"kind: {job.kind}", f"state: {state}"]
    if job.finished_at is not None:
        parts.append(f"returncode: {job.returncode}")
        parts.append(f"duration_s: {job.finished_at - job.started_at:.2f}")
    else:
        parts.append(f"runtime_s: {time.time() - job.started_at:.2f}")
    return "\n".join(parts)


def job_wait(job_id: str, timeout: int = 90) -> str:
    """Block until a background job finishes or timeout elapses, then return status.

    Useful pattern: kick off run_in_background, then call job_wait so the chat
    appears to "wait until done" within a single response.

    Args:
        job_id: The job to wait on.
        timeout: Seconds to wait at most.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return f"[job_wait error: unknown job_id {job_id!r}]"
    try:
        if job.process is not None:
            job.process.wait(timeout=timeout)
        job.refresh()
        return job_status(job_id)
    except subprocess.TimeoutExpired:
        return f"[job_wait timeout: job {job_id} still running after {timeout}s. Use job_status to recheck.]"
    except Exception as e:
        return f"[job_wait error: {type(e).__name__}: {e}]"


def job_output(job_id: str, max_chars: int = 8000, stream: str = "both") -> str:
    """Return the captured stdout/stderr of a background job.

    Args:
        job_id: The job.
        max_chars: Truncate each stream to this many characters.
        stream: "stdout", "stderr", or "both".
    """
    job = _JOBS.get(job_id)
    if job is None:
        return f"[job_output error: unknown job_id {job_id!r}]"
    out, err = "", ""
    try:
        if job.stdout_path and Path(job.stdout_path).exists():
            out = Path(job.stdout_path).read_text(encoding="utf-8", errors="replace")
        if job.stderr_path and Path(job.stderr_path).exists():
            err = Path(job.stderr_path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[job_output error: {type(e).__name__}: {e}]"
    parts = []
    if stream in {"stdout", "both"}:
        if len(out) > max_chars:
            out = out[-max_chars:] + f"\n... (truncated, showing last {max_chars} chars)"
        parts.append(f"[stdout]\n{out or '(empty)'}")
    if stream in {"stderr", "both"}:
        if len(err) > max_chars:
            err = err[-max_chars:] + f"\n... (truncated, showing last {max_chars} chars)"
        parts.append(f"[stderr]\n{err or '(empty)'}")
    return "\n".join(parts)


def job_list() -> str:
    """List all background jobs in this server's memory."""
    with _LOCK:
        jobs = list(_JOBS.values())
    if not jobs:
        return "(no jobs)"
    for j in jobs:
        j.refresh()
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    lines = []
    for j in jobs:
        state = "running" if j.is_running() else f"done(rc={j.returncode})"
        age = time.time() - j.started_at
        lines.append(f"{j.id}  {state:<14}  {age:>6.1f}s  {j.label}")
    return "\n".join(lines)


def job_kill(job_id: str) -> str:
    """Terminate a running background job."""
    job = _JOBS.get(job_id)
    if job is None:
        return f"[job_kill error: unknown job_id {job_id!r}]"
    if job.process is None or not job.is_running():
        return f"[job_kill: job {job_id} is not running]"
    try:
        job.process.terminate()
        try:
            job.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.process.kill()
        job.refresh()
        return f"Killed job {job_id}"
    except Exception as e:
        return f"[job_kill error: {type(e).__name__}: {e}]"
