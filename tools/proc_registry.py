# -*- coding: utf-8 -*-
"""Which OS processes a fleet worker's tool calls started, so they can be killed when the
worker ends.

THE INCIDENT (measured 2026-11-02 21:09-21:15). A job family (the UI-automation A/B test) had
its workers CLOSED/CANCELLED by the operator, yet the processes those workers had started kept
running on the real desktop for 20+ minutes, opening Notepad and Calculator windows:
python.exe running harness_d.py / run_all_c.py / run_g.py, and powershell.exe driving
UIAutomationClient. Closing a worker only stopped the Copilot conversation; the OS processes
its run_python / run_powershell / shell_exec tool calls had spawned were ORPHANS -- nothing
owned them, nothing killed them.

WHY A REGISTRY AND NOT JUST ANCESTRY. relay/orphan_reaper.py already attributes a leftover
build process by its ancestry and work-tree path, but it only fires at 2h+ of age and only for
processes whose command line names a worktree. The desktop automation above named no worktree
and had to die in SECONDS, not hours. The honest, prompt attribution is the one recorded at the
moment of the spawn: this run_python call, made inside THIS worker's turn, started THIS pid.
That is what this module records.

WHAT MAKES A KILL SAFE. Only a process THIS registry recorded, and only when its pid AND its
creation time still match the record. The creation time is the guard against PID REUSE: an
unrelated process that happens to inherit a recycled pid has a different creation time, so it is
never matched and never killed. The fleet runner, the MCP server and the supervisor are never
in the registry (they are not started by a worker's tool call), so they are never candidates.
This is provenance, exactly like orphan_reaper's rule -- "do not touch a process you did not
start" -- but recorded at the source instead of reconstructed later.

DURABLE, APPEND-ONLY, PER WORKER. One JSONL file per worker (or per job id) under
.fleet/procs/<worker>.jsonl, one line per spawn. Durable so a sweep after a restart can still
find the pids a worker that no longer exists left behind. Append-only for the same reason
tool_ledger is: a record written before the kill is a record that survives the thing that
killed it.

NOTHING HERE RAISES INTO A TOOL CALL. register() is called on the hot path of every spawning
tool; a bookkeeping failure must never stop a tool from running or break a worker's turn, so
every public function is total.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time

__all__ = [
    "registry_dir", "set_worker", "current_worker", "worker_scope",
    "register", "registered", "kill_worker_tree",
]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The worker whose tool call is running on THIS thread/context right now. Set by the gateway
#: (main.py) from the `_worker` it already pops for the ledger, and read by register() so a
#: spawning tool does not have to thread the name through its own signature. A ContextVar, not
#: a thread-local, so it follows the gateway's async dispatch correctly and defaults to "".
_CURRENT_WORKER: contextvars.ContextVar = contextvars.ContextVar("mcp_proc_worker", default="")

_LOCK = threading.Lock()

#: A worker name becomes a file name; keep it to safe characters and bound it, the same way
#: relay/temp_home.temp_dir sanitises a scope. An empty or all-unsafe name maps to "_shared".
_BAD = re.compile(r"[^A-Za-z0-9._-]")


def registry_dir() -> str:
    """`.fleet/procs` under the repo, resolved at call time so the test isolation fixture can
    move the repo root. Not created here; the writer creates it on first append."""
    return os.path.join(_repo(), ".fleet", "procs")


def _repo() -> str:
    """Resolved at call time. A test (and the repo-wide isolation fixture) overrides
    MCP_PROC_REGISTRY_DIR or the module _REPO to redirect the whole tree."""
    override = os.environ.get("MCP_PROC_REGISTRY_REPO")
    return override or _REPO


def _safe(name) -> str:
    seg = _BAD.sub("_", str(name or "")).strip("._-")
    return seg or "_shared"


def _path_for(worker) -> str:
    return os.path.join(registry_dir(), _safe(worker) + ".jsonl")


# --- worker identity on the hot path --------------------------------------------------------
def set_worker(worker: str):
    """Bind the current worker for this context; returns the ContextVar token to reset with.

    The gateway calls this right after it pops `_worker` for the ledger and resets it in its
    finally, so every spawn made while the tool runs is attributed to the right worker and a
    later call on the same thread is not.
    """
    return _CURRENT_WORKER.set(str(worker or ""))


def reset_worker(token) -> None:
    """Undo a set_worker(). Total: a bad or already-reset token is ignored."""
    try:
        _CURRENT_WORKER.reset(token)
    except Exception:
        try:
            _CURRENT_WORKER.set("")
        except Exception:
            pass


def current_worker() -> str:
    try:
        return _CURRENT_WORKER.get() or ""
    except Exception:
        return ""


class worker_scope:
    """`with worker_scope(name):` binds the worker for the block and resets it on exit.

    A convenience for callers that are not the gateway (tests, a direct spawn site); the gateway
    uses set_worker/reset_worker because its begin and finally are far apart.
    """

    def __init__(self, worker: str):
        self._worker = worker
        self._token = None

    def __enter__(self):
        self._token = set_worker(self._worker)
        return self

    def __exit__(self, *exc):
        reset_worker(self._token)
        return False


# --- creation time (the PID-reuse guard) ----------------------------------------------------
def _create_time(pid: int):
    """The process's creation time as a float, or None when it cannot be read.

    psutil is in requirements.txt and is how the rest of this repo reads process facts; its
    create_time() is comparable across time on both Windows and POSIX. None (process already
    gone, or psutil unavailable) means "cannot vouch for identity": such a record is still
    written, but a later kill will only proceed when a fresh read matches it, so a missing
    create time fails SAFE -- it is never treated as a match.
    """
    try:
        import psutil
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


# --- writing ---------------------------------------------------------------------------------
def register(pid: int, worker: str = None, tool: str = "") -> bool:
    """Record that `worker`'s tool call started process `pid`. Never raises; returns whether a
    row was written.

    `worker` defaults to the ContextVar the gateway set. A spawn made outside any worker turn
    (a CLI run, a test without a scope) has no worker and is NOT recorded: an unattributed pid
    is exactly the thing this must never later kill on a guess.
    """
    try:
        who = worker if worker is not None else current_worker()
        who = str(who or "")
        if not who:
            return False
        try:
            p = int(pid)
        except Exception:
            return False
        if p <= 0:
            return False
        row = {
            "pid": p,
            "create_time": _create_time(p),
            "tool": str(tool or "")[:80],
            "ts": time.time(),
            "owner_pid": os.getpid(),
        }
        path = _path_for(who)
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def registered(worker: str):
    """Every (pid, create_time, tool, ts) row recorded for `worker`, in order. [] on any error
    or when the worker has no file."""
    rows = []
    try:
        path = _path_for(worker)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    except Exception:
        return []
    return rows


# --- killing (the one dangerous verb, fenced) -----------------------------------------------
def _still_the_same(pid: int, recorded_create_time) -> bool:
    """True only when `pid` is alive AND its creation time matches what was recorded.

    THE PID-REUSE GUARD, AND IT FAILS SAFE. If the recorded creation time is None (could not be
    read at spawn) or the live one cannot be read now, this returns False: a process whose
    identity cannot be confirmed is left alone. Only an exact float match is a match.
    """
    try:
        if recorded_create_time is None:
            return False
        import psutil
        if not psutil.pid_exists(int(pid)):
            return False
        live = float(psutil.Process(int(pid)).create_time())
        return abs(live - float(recorded_create_time)) < 0.0005
    except Exception:
        return False


def _kill_tree_by_pid(pid: int, timeout_s: float = 5.0) -> bool:
    """Kill `pid` and every descendant it has right now. Returns whether the root is gone.

    Uses psutil to enumerate children BEFORE killing the root, so a grandchild that would be
    reparented when its parent dies is still in the list. Children are killed first, then the
    root, then we wait. Never raises.
    """
    try:
        import psutil
    except Exception:
        return _kill_tree_fallback(pid, timeout_s)
    try:
        root = psutil.Process(int(pid))
    except Exception:
        return True  # already gone
    try:
        procs = root.children(recursive=True)
    except Exception:
        procs = []
    procs.append(root)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    gone, _alive = psutil.wait_procs(procs, timeout=max(0.1, float(timeout_s)))
    try:
        return not psutil.pid_exists(int(pid))
    except Exception:
        return True


def _kill_tree_fallback(pid: int, timeout_s: float = 5.0) -> bool:
    """No psutil: taskkill /T on Windows, killpg best-effort on POSIX. Never raises."""
    import subprocess
    try:
        if os.name == "nt":
            from tools import childproc
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=max(1.0, float(timeout_s)),
                           creationflags=childproc.headless_creationflags())
        else:
            import signal
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except Exception:
                os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass
    return True


def kill_worker_tree(worker: str, timeout_s: float = 5.0) -> dict:
    """Terminate the still-alive process trees this worker's tool calls started. Never raises.

    For every row recorded for `worker`, the process is killed ONLY when its pid and creation
    time still match (``_still_the_same``). A row whose process is gone, or whose identity
    cannot be confirmed, is skipped. Returns a report:

        {"worker": str, "killed": [pid, ...], "skipped": [{pid, why}, ...],
         "candidates": int}

    The report names what it did before it is trusted, the same discipline orphan_reaper keeps.
    """
    report = {"worker": str(worker or ""), "killed": [], "skipped": [], "candidates": 0}
    try:
        rows = registered(worker)
        report["candidates"] = len(rows)
        seen = set()
        for r in rows:
            try:
                pid = int(r.get("pid"))
            except Exception:
                continue
            ct = r.get("create_time")
            key = (pid, ct)
            if key in seen:
                continue
            seen.add(key)
            if not _still_the_same(pid, ct):
                report["skipped"].append({"pid": pid, "why": "gone or identity unconfirmed"})
                continue
            ok = _kill_tree_by_pid(pid, timeout_s=timeout_s)
            if ok:
                report["killed"].append(pid)
            else:
                report["skipped"].append({"pid": pid, "why": "kill did not confirm"})
    except Exception:
        pass
    return report


def sweep_dead_owners(timeout_s: float = 5.0) -> dict:
    """Kill registered trees whose OWNING server process is no longer alive. Safe to run while
    other servers/runs are live. Never raises.

    WHY OWNER-SCOPED, NOT WORKER-SCOPED. The worker-scoped sweep() needs the live worker list,
    which the coordinator only knows mid-run; at RUN START the honest, run-independent fact is
    "the server that spawned this process is dead". Each row records owner_pid (the MCP server
    pid at spawn). If that server pid is gone, its children are orphans and are reaped. If it is
    still alive -- including a DIFFERENT server running a concurrent run -- its rows are left
    entirely alone, so this never reaches into another live run's processes.

    As always the kill itself re-checks pid AND creation time, so even an orphan row only kills
    a process whose identity still matches. Returns {"reaped": [...], "kept_owners": [...]}.
    """
    out = {"reaped": [], "kept_owners": []}
    try:
        import psutil
        d = registry_dir()
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for fn in names:
            if not fn.endswith(".jsonl"):
                continue
            stem = fn[:-len(".jsonl")]
            rows = registered(stem)
            # Group this worker's rows by owning server pid; an owner still alive keeps ALL its
            # rows (it may be mid-run), a dead owner's rows are candidates for the kill.
            owners = {}
            for r in rows:
                owners.setdefault(r.get("owner_pid"), []).append(r)
            any_live_owner = False
            reaped_here = []
            for owner_pid, grp in owners.items():
                alive = False
                try:
                    alive = owner_pid is not None and psutil.pid_exists(int(owner_pid))
                except Exception:
                    alive = False
                if alive:
                    any_live_owner = True
                    if int(owner_pid) not in [int(o) for o in out["kept_owners"]
                                              if o is not None]:
                        out["kept_owners"].append(int(owner_pid))
                    continue
                for r in grp:
                    try:
                        pid = int(r.get("pid"))
                    except Exception:
                        continue
                    if _still_the_same(pid, r.get("create_time")):
                        if _kill_tree_by_pid(pid, timeout_s=timeout_s):
                            reaped_here.append(pid)
            if reaped_here:
                out["reaped"].append({"worker": stem, "killed": reaped_here})
            # Remove a file only when no owner is still live for it; a live owner may still be
            # appending, so its file must survive this sweep.
            if not any_live_owner:
                forget(stem)
    except Exception:
        pass
    return out


def forget(worker: str) -> bool:
    """Delete a worker's registry file once its trees are reaped. Never raises; returns whether
    a file was removed. A kept-around file is only a stale record (every kill re-checks identity
    before acting), but removing it after a clean close keeps the sweep's work bounded."""
    try:
        os.remove(_path_for(worker))
        return True
    except OSError:
        return False
    except Exception:
        return False
