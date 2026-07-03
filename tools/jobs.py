import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ._subproc import sanitized_child_env
from .file_ops import _validate_path
from .security import require_unlocked

# ---------------------------------------------------------------------------
# Background-job watchdog cap (MCP spec §21.6 fix B.2)
#
# run_in_background / run_python_in_background are fire-and-return (Popen,
# return job_id immediately) so they can't rely on subprocess.run's timeout=.
# Instead each job gets a daemon threading.Timer that terminate()s then
# kill()s it if it's still running once the cap elapses. Overridable via
# MCP_JOB_MAX_RUNTIME_S for tests / special cases.
# ---------------------------------------------------------------------------
_JOB_MAX_RUNTIME_S = float(os.environ.get("MCP_JOB_MAX_RUNTIME_S", "3600"))

# Sentinel returncode recorded when the watchdog had to kill a job, so
# job_status / job_output can explain why it ended instead of just showing a
# bare returncode.
_WATCHDOG_KILL_RC = -9999


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
        "watchdog",
        "killed_by_watchdog",
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
        self.watchdog: Optional[threading.Timer] = None
        self.killed_by_watchdog: bool = False

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def refresh(self) -> None:
        if self.process is None or self.finished_at is not None:
            return
        rc = self.process.poll()
        if rc is not None:
            self.returncode = rc
            self.finished_at = time.time()
            _cancel_watchdog(self)

    def cancel_watchdog(self) -> None:
        _cancel_watchdog(self)


def _cancel_watchdog(job: "_Job") -> None:
    """Cancel and drop the job's watchdog timer so it never leaks a thread."""
    t = job.watchdog
    if t is not None:
        job.watchdog = None
        try:
            t.cancel()
        except Exception:
            pass


def _watchdog_fire(job: "_Job") -> None:
    """Timer callback: if the job is still running past the cap, kill it."""
    job.watchdog = None  # this timer has fired; nothing left to cancel
    proc = job.process
    if proc is None or proc.poll() is not None:
        return  # already finished naturally
    # Mark the decision to kill BEFORE terminate(): terminate() (SIGTERM on POSIX)
    # can make the child exit almost instantly, so a concurrent job_status poll could
    # otherwise observe "finished" via poll() before the finally-block set the flag,
    # and miss the "killed: exceeded max runtime" note. Setting it here closes that race.
    job.killed_by_watchdog = True
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass
    finally:
        job.killed_by_watchdog = True
        job.refresh()
        # refresh() only sets returncode/finished_at if the process actually
        # reports a code; make sure both are set even if poll() lagged.
        if job.finished_at is None:
            job.finished_at = time.time()
        if job.returncode is None:
            job.returncode = proc.poll() if proc is not None else _WATCHDOG_KILL_RC


def _start_watchdog(job: "_Job", cap_s: Optional[float] = None) -> None:
    """Arm a daemon timer that kills `job` if it outlives cap_s seconds."""
    cap = _JOB_MAX_RUNTIME_S if cap_s is None else cap_s
    if cap <= 0:
        return
    t = threading.Timer(cap, _watchdog_fire, args=(job,))
    t.daemon = True
    job.watchdog = t
    t.start()


_JOBS: dict[str, _Job] = {}
_LOCK = threading.Lock()

# Light, bounded cleanup so the in-memory job table can't grow without limit over a
# long-lived server. Finished jobs older than _JOB_TTL_S are dropped; if the table is
# still over _JOB_MAX after that, the oldest finished jobs are evicted. Running jobs are
# never evicted. Caller must hold _LOCK.
_JOB_TTL_S = 6 * 3600
_JOB_MAX = 500


def _prune_jobs_locked() -> None:
    now = time.time()
    for jid, j in list(_JOBS.items()):
        j.refresh()
        if j.finished_at is not None and (now - j.finished_at) > _JOB_TTL_S:
            del _JOBS[jid]
    if len(_JOBS) > _JOB_MAX:
        finished = [
            (j.finished_at, jid)
            for jid, j in _JOBS.items()
            if j.finished_at is not None
        ]
        finished.sort()
        for _, jid in finished[: len(_JOBS) - _JOB_MAX]:
            _JOBS.pop(jid, None)


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
    from . import contract_gate as _cg
    # Same gate code_exec.shell_exec applies to its foreground command — closes the
    # gate-bypass where a destructive command routed through run_in_background instead
    # of shell_exec skipped HITL approval entirely. Reuses destructive_shell/check_op
    # verbatim; no detection logic duplicated here.
    if _cg.destructive_shell(command):
        _g = _cg.check_op("shell_destructive", command[:200])
        if _g is not None:
            return _g
    try:
        cwd = os.getcwd()
        if working_dir:
            p = _validate_path(working_dir)
            if not p.is_dir():
                return f"[run_in_background error: not a directory: {p}]"
            cwd = str(p)
        out_path, err_path = _create_log_paths()
        # Close the parent's file handles right after Popen inherits them, or the
        # server leaks two FDs per background job until process exit -> with the rest
        # of the CLOSE_WAIT pile-up this contributes to FD exhaustion. The child keeps
        # its own inherited copies; job_output reads the files back by path.
        with open(out_path, "w", encoding="utf-8") as out_f, open(
            err_path, "w", encoding="utf-8"
        ) as err_f:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=out_f,
                stderr=err_f,
                cwd=cwd,
                env=sanitized_child_env(),
            )
        job = _Job("shell", label or command[:60], command)
        job.process = proc
        job.stdout_path = out_path
        job.stderr_path = err_path
        with _LOCK:
            _JOBS[job.id] = job
            _prune_jobs_locked()
        _start_watchdog(job)
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
    from . import contract_gate as _cg
    # Same gate code_exec.run_python applies to its foreground code — destructive
    # ops expressed as shell text OR Python source both route through the existing
    # 'shell_destructive' op_class, exactly mirroring run_python. Reuses
    # destructive_shell/destructive_python/check_op verbatim; no detection logic
    # duplicated here.
    if _cg.destructive_shell(code) or _cg.destructive_python(code):
        _g = _cg.check_op("shell_destructive", code[:200])
        if _g is not None:
            return _g
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            script_path = f.name
        out_path, err_path = _create_log_paths()
        # Close parent FDs after Popen inherits them (see run_in_background) to avoid a
        # two-FD-per-job leak.
        with open(out_path, "w", encoding="utf-8") as out_f, open(
            err_path, "w", encoding="utf-8"
        ) as err_f:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=out_f,
                stderr=err_f,
                env=sanitized_child_env(),
            )
        job = _Job("python", label or "python script", script_path)
        job.process = proc
        job.stdout_path = out_path
        job.stderr_path = err_path
        with _LOCK:
            _JOBS[job.id] = job
            _prune_jobs_locked()
        _start_watchdog(job)
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
        if job.killed_by_watchdog:
            parts.append(
                f"killed: exceeded max runtime ({_JOB_MAX_RUNTIME_S:.0f}s cap, "
                f"MCP_JOB_MAX_RUNTIME_S)"
            )
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
    locked = require_unlocked()
    if locked:
        return locked
    job = _JOBS.get(job_id)
    if job is None:
        return f"[job_kill error: unknown job_id {job_id!r}]"
    if job.process is None or not job.is_running():
        return f"[job_kill: job {job_id} is not running]"
    try:
        # Cancel the watchdog first so it doesn't race this explicit kill and
        # mislabel a human-requested kill as "exceeded max runtime".
        job.cancel_watchdog()
        job.process.terminate()
        try:
            job.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.process.kill()
        job.refresh()
        return f"Killed job {job_id}"
    except Exception as e:
        return f"[job_kill error: {type(e).__name__}: {e}]"
