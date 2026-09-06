"""Typed-job task router -- the delegation substrate ("足回り").

A commander (M365 Copilot, the cockpit, a cron, or the Claude agent) drops a typed job into the
queue; the router dispatches each job to the executor whose *hands* actually fit the task, then
writes the result back. Three destinations:

  LOCAL   -- run right here in this process: shell / python / screenshot / file ops. Fast, has the
             local machine + MCP-free screen capture. This is where "take a screenshot" belongs --
             M365 cannot see the local screen.
  FLEET   -- hand to M365 Copilot via the relay fleet: coding solves and web research. M365 is the
             strong general agent for "solve this issue" / "research X on the web".
  CLAUDE  -- escalate to the Claude agent (fable): the deep-research skill (fan-out + cited +
             adversarial verify) and anything needing MCP tools / skills the fleet/script lack.

Queue layout (all under .fleet/tasks/):
  pending/<id>.json   incoming jobs           {id, type, payload, created}
  running/<id>.json   claimed (in progress)
  awaiting/<id>.json  LOCAL job held for human approval (see approval gate below)
  done/<id>.json      the ROUTER is finished with it {..., status, result, error, ts_done}
  for_fleet/<id>.txt  FLEET handoff (a goal the relay fleet consumes)
  for_claude/<id>.json CLAUDE handoff (escalation the agent picks up)

  `done/` DOES NOT MEAN THE WORK IS DONE. It means this router will not look at the job
  again; the job's own `status` says what actually happened. A fleet_goal that arrived with
  no run in flight is written here with status="awaiting_fleet" and ts_done=null while the
  thing that still has to happen sits in for_fleet/<id>.txt under a different extension. This
  line used to read "finished", and a careful reader auditing the queue read the record in
  done/, saw no matching *.delivered.json, and reported a job that had moved out of for_fleet/
  by some route they could not determine. Nothing had moved: the record and the handoff are
  two artifacts written in the same pass, and only the handoff is the outstanding work.

Design notes:
  * One writer claims a job by moving pending/ -> running/ (atomic rename) so two routers never
    double-run a job.
  * LOCAL executors are bounded (timeout) and never raise out of run_job -- a failure becomes a
    {status:"error"} result, so one bad job can't wedge the loop.
  * CLAUDE jobs are not executed here; they're written as a handoff artifact and marked
    {status:"escalated"} -- the Claude agent completes them and writes done/.
  * FLEET jobs ARE delivered here, and until 2026-09-05 they were not: this branch wrote
    for_fleet/<id>.txt, said "dispatched" and stopped, and nothing anywhere read that
    directory. A goal now joins the run that is in flight, by appending add_goal to
    commands.json exactly as relay/code_task.py does -- because two fleets share the one
    dedicated Edge and clobber each other's status.json. With no run in flight the job says
    "awaiting_fleet" and waits; starting a fleet is opt-in (FLEET_INTAKE_AUTOSTART), since
    spawning one opens a browser and spends the tenant's Copilot budget.
  * LOCAL jobs pass through a 3-mode approval gate (TASK_JOB_APPROVAL_MODE, see job_gate()
    below) before they execute -- a CONFIRM decision moves the job to awaiting/ and raises a
    desktop-notification HITL gate instead of running it or blocking the dispatch loop.
"""
import contextlib
import hashlib
import itertools
import json
import os
import subprocess
import sys
import threading
import uuid
import time
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(REPO, ".fleet", "tasks")
SUBDIRS = ("pending", "running", "done", "awaiting", "for_fleet", "for_claude")
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
APPROVED_JOBS_FILE = os.path.join(REPO, ".fleet", "approved_jobs.json")

# type -> destination. Unknown types default to CLAUDE (the most capable fallback).
DESTINATION = {
    "shell": "local",
    "python": "local",
    "screenshot": "local",
    "file": "local",
    "coding": "fleet",
    "research": "fleet",        # ordinary web research -> M365 (it has web access)
    # An instruction handed in through the MCP door by an agent -- see tools/fleet_intake.py.
    # It is its own type rather than borrowed from "coding" because the destination is the
    # only thing known about it: what kind of work it is has not been decided by anyone yet,
    # and labelling it "coding" would be the router asserting something it was not told.
    # Named explicitly because DEFAULT_DESTINATION is claude, so an unlisted type would go
    # somewhere the caller did not ask for.
    "fleet_goal": "fleet",
    "deep-research": "claude",  # rigorous, cited, adversarially-verified -> the skill
}
DEFAULT_DESTINATION = "claude"

# A job may force its destination with payload {"escalate": true} -> CLAUDE, regardless of type.
LOCAL_TIMEOUT_S = int(os.environ.get("TASK_LOCAL_TIMEOUT_S", "120"))

# ── Job-approval gate (Claude-Code-style 3-mode) ───────────────────────────────────────────────
# A job dropped into .fleet/tasks/pending gives LOCAL shell/python/file execution. Without this
# gate that is unconditional arbitrary execution for anyone who can write a job file. Modes:
#   default -- first time a job CLASS is seen it always confirms (desktop-notification gate,
#              job held in awaiting/); once a human approves that class, later same-class jobs
#              auto-run -- UNLESS the specific payload is itself flagged destructive (see
#              job_gate() below: an approved class never bypasses a fresh destructive check).
#   auto    -- purely STATIC risk check (never executes the payload to test it): clean -> run,
#              a STOP-pattern -> deny, an ASK-pattern -> fall back to a confirm gate.
#   bypass  -- current (pre-gate) behavior: run anything. The H3 path floor (see _exec_file /
#              _resolve_file_path) still applies in this mode -- it is not part of the 3-mode
#              gate, it is an always-on hard floor.
# SHIPPED DEFAULT = "default" so a fresh install is protected out of the box.
TASK_JOB_APPROVAL_MODE = os.environ.get("TASK_JOB_APPROVAL_MODE", "default").strip().lower()
if TASK_JOB_APPROVAL_MODE not in ("default", "auto", "bypass"):
    TASK_JOB_APPROVAL_MODE = "default"

# REPO is already computed above; make sure it's importable as a package root so the
# absolute imports below (tools.*, relay.*) resolve regardless of how this module was
# launched (`python relay/task_router.py` puts relay/ on sys.path[0], not REPO).
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# These helpers are reused, not reimplemented (see module docstring / design notes below).
# Imports are defensive: the router must stay importable even if tools/relay siblings are
# absent from a stripped-down deployment -- job_gate() then degrades to "never flags risk",
# which only matters in "auto" mode; "default" mode still gates on first-seen-class alone.
try:
    from tools.contract_gate import destructive_shell as _destructive_shell
    from tools.contract_gate import destructive_python as _destructive_python
except Exception:
    def _destructive_shell(_text):
        return False

    def _destructive_python(_text):
        return False

try:
    from relay.autonomy_gate import _STOP_PATTERNS, _ASK_PATTERNS, _matches as _autonomy_matches
except Exception:
    _STOP_PATTERNS = ()
    _ASK_PATTERNS = ()

    def _autonomy_matches(_text, _patterns):
        return []

try:
    from tools.file_ops import _validate_path, ALLOWED_BASE
except Exception:
    _validate_path = None
    ALLOWED_BASE = None

try:
    from tools.notify_ops import notify_approval_gate
except Exception:
    def notify_approval_gate(_title, _body, _gate_path):
        return None

try:
    from tools.approval_policy import current_approval_mode as _current_approval_mode
except Exception:
    def _current_approval_mode(default=None):
        return default or "default"


def destination_for(job):
    """Resolve a job to LOCAL / FLEET / CLAUDE. Explicit payload.escalate wins (force CLAUDE)."""
    payload = job.get("payload") or {}
    if isinstance(payload, dict) and payload.get("escalate"):
        return "claude"
    return DESTINATION.get(job.get("type", ""), DEFAULT_DESTINATION)


def ensure_dirs():
    for d in SUBDIRS:
        os.makedirs(os.path.join(TASKS, d), exist_ok=True)


def _p(sub, name):
    return os.path.join(TASKS, sub, name)


# ── LOCAL executors (bounded; return a (status, result, error) tuple) ─────────────────────────

def _exec_shell(payload):
    cmd = payload.get("cmd") or payload.get("command")
    if not cmd:
        return "error", None, "shell job missing 'cmd'"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       errors="replace", timeout=LOCAL_TIMEOUT_S, cwd=REPO)
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return ("ok" if r.returncode == 0 else "error"), {"rc": r.returncode, "output": out[:20000]}, None


def _exec_python(payload):
    code = payload.get("code")
    if not code:
        return "error", None, "python job missing 'code'"
    py = VENVPY if os.path.isfile(VENVPY) else sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                       errors="replace", timeout=LOCAL_TIMEOUT_S, cwd=REPO, env=env)
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return ("ok" if r.returncode == 0 else "error"), {"rc": r.returncode, "output": out[:20000]}, None


_CAPTURE_PS1 = os.path.join(REPO, "relay", "capture.ps1")


def _exec_screenshot(payload):
    """Capture to a PNG via relay/capture.ps1 (PowerShell + .NET, no MCP unlock needed). payload:
      out      -- save path (default .fleet/tasks/done/shot_<id>.png)
      proc     -- capture ONLY the window of this PROCESS (e.g. 'FleetCockpit') -- robust when the
                  window has no title
      window   -- capture ONLY the window whose TITLE contains this substring
      region   -- [l,t,w,h] pixel region of the virtual screen
      (none)   -- full virtual screen (all monitors)
    Precedence proc > window > region. This is the clean window-targeted path so a README shot
    needs no manual cropping."""
    out = os.path.abspath(payload.get("out") or _p("done", "shot_%s.png" % payload.get("id", "x")))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", _CAPTURE_PS1, "-Out", out]
    if payload.get("proc"):
        cmd += ["-Proc", str(payload["proc"])]
    elif payload.get("window"):
        cmd += ["-Window", str(payload["window"])]
    elif payload.get("region") and len(payload["region"]) == 4:
        cmd += ["-Region", ",".join(str(int(x)) for x in payload["region"])]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       timeout=LOCAL_TIMEOUT_S)
    if r.returncode == 0 and os.path.isfile(out):
        return "ok", {"path": out, "mode": (r.stdout or "").strip()[:80]}, None
    return "error", None, ((r.stderr or r.stdout or "screenshot failed")[:2000])


def _resolve_file_path(path, allow_outside):
    """Resolve `path` and enforce the H3 hard floor. ALWAYS ON, mode-independent -- this
    is NOT part of the 3-mode approval gate above; it applies in every mode, including
    "bypass". Threat model: anyone who can write a job file into .fleet/tasks/pending gets
    this code path, independent of whatever MCP_ALLOWED_BASE happens to be (which may be
    default-open / unrestricted -- see tools/file_ops.py's default-open policy). So on top
    of tools.file_ops._validate_path() (which honors MCP_ALLOWED_BASE when it IS restricted),
    a file job must resolve under REPO (or an explicit TASK_FILE_ALLOWED_BASE env root)
    unless the payload sets an explicit allow_outside=true.

    Returns (resolved_path_str, None) on success, or (None, error_message) on rejection.
    """
    try:
        if _validate_path is not None:
            resolved = str(_validate_path(path))
        else:
            resolved = str(Path(path).expanduser().resolve())
    except PermissionError as e:
        return None, "path rejected: %s" % e
    except Exception as e:
        return None, "path validation failed: %s: %s" % (type(e).__name__, e)
    if not allow_outside:
        allowed_root = os.environ.get("TASK_FILE_ALLOWED_BASE", "").strip() or REPO
        try:
            allowed_root_r = os.path.realpath(allowed_root)
            resolved_r = os.path.realpath(resolved)
            if os.path.commonpath([allowed_root_r, resolved_r]) != allowed_root_r:
                return None, ("file path %r is outside the allowed root %r "
                              "(set payload.allow_outside=true to override)" % (resolved, allowed_root_r))
        except ValueError:
            # commonpath raises on e.g. mixed Windows drive letters -- definitely outside
            return None, ("file path %r is outside the allowed root %r "
                          "(different drive)" % (resolved, allowed_root))
    return resolved, None


def _exec_file(payload):
    """Read or write a file. payload {op:'read'|'write', path, content?, allow_outside?}."""
    op = payload.get("op")
    path = payload.get("path")
    if not path:
        return "error", None, "file job missing 'path'"
    try:
        resolved, err = _resolve_file_path(path, payload.get("allow_outside"))
    except Exception as e:
        return "error", None, "path validation failed: %s: %s" % (type(e).__name__, e)
    if err:
        return "error", None, err
    path = resolved
    if op == "read":
        with open(path, encoding="utf-8", errors="replace") as f:
            return "ok", {"content": f.read()[:50000]}, None
    if op == "write":
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload.get("content", ""))
        return "ok", {"path": os.path.abspath(path)}, None
    return "error", None, "file job 'op' must be read|write"


LOCAL_EXECUTORS = {
    "shell": _exec_shell, "python": _exec_python,
    "screenshot": _exec_screenshot, "file": _exec_file,
}


# ── Job-approval gate: class key, allowlist store, static risk, decision ──────────────────────

def _job_class_key(job_type, payload):
    """Pure fn: map (job_type, payload) -> a stable class-key string for the approval
    allowlist. Deliberately granular: "git status" and "git push --force" are DISTINCT
    classes -- collapsing to just the program name (e.g. "git") would let one approval of
    a benign invocation silently cover a destructive one later. shell/python key on the
    first two whitespace-normalized tokens of the command/code; file keys on
    (op, resolved parent dir). Hermetically testable: no I/O besides path resolution."""
    payload = payload or {}
    if job_type == "shell":
        cmd = (payload.get("cmd") or payload.get("command") or "").split()
        return "shell::%s" % " ".join(cmd[:2])
    if job_type == "python":
        code = (payload.get("code") or "").split()
        return "python::%s" % " ".join(code[:2])
    if job_type == "file":
        op = payload.get("op") or ""
        path = payload.get("path") or ""
        try:
            parent = os.path.dirname(os.path.abspath(os.path.expanduser(str(path))))
        except Exception:
            parent = ""
        return "file::%s::%s" % (op, parent)
    return "%s::" % job_type


def _load_approved_jobs():
    """Return {"classes": {key: {approved_at, example}}}. Tolerates a missing or corrupt
    store file -- always returns a well-formed dict, never raises."""
    try:
        with open(APPROVED_JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("classes"), dict):
            return data
    except Exception:
        pass
    return {"classes": {}}


def _save_approved_jobs(data):
    """Atomic write (tmp + os.replace) so a crash mid-write can't corrupt the store."""
    try:
        os.makedirs(os.path.dirname(APPROVED_JOBS_FILE) or ".", exist_ok=True)
        tmp = APPROVED_JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, APPROVED_JOBS_FILE)
    except Exception:
        pass


def _is_class_approved(key):
    return key in (_load_approved_jobs().get("classes") or {})


def _approve_class(key, example=""):
    data = _load_approved_jobs()
    classes = data.setdefault("classes", {})
    classes[key] = {"approved_at": time.time(), "example": str(example)[:200]}
    _save_approved_jobs(data)


def _static_risk(job_type, payload):
    """Static risk check -- NEVER executes the payload. Returns (level, reason) with
    level in {"clean", "ask", "stop"}. Reuses tools.contract_gate's deterministic regex
    classifiers (tuned not to false-positive on pytest / git status / git add / commit /
    builds) plus relay.autonomy_gate's broader push/deploy/secrets/PII vocabulary."""
    payload = payload or {}
    if job_type == "shell":
        cmd = payload.get("cmd") or payload.get("command") or ""
        if _destructive_shell(cmd):
            return "stop", "matched destructive-shell pattern"
        if _autonomy_matches(cmd, _STOP_PATTERNS):
            return "stop", "matched autonomy STOP pattern"
        if _autonomy_matches(cmd, _ASK_PATTERNS):
            return "ask", "matched autonomy ASK pattern"
        return "clean", ""
    if job_type == "python":
        code = payload.get("code") or ""
        if _destructive_python(code):
            return "stop", "matched destructive-python pattern"
        if _autonomy_matches(code, _STOP_PATTERNS):
            return "stop", "matched autonomy STOP pattern"
        if _autonomy_matches(code, _ASK_PATTERNS):
            return "ask", "matched autonomy ASK pattern"
        return "clean", ""
    if job_type == "file":
        path = payload.get("path") or ""
        try:
            _resolved, err = _resolve_file_path(path, payload.get("allow_outside"))
        except Exception as e:
            return "ask", "path validation failed: %s: %s" % (type(e).__name__, e)
        if err:
            return "ask", err
        return "clean", ""
    return "clean", ""


def job_gate(job_type, payload, mode):
    """Decide ALLOW / CONFIRM / DENY for a LOCAL job before it runs. NEVER executes the
    payload to test it -- purely static analysis + the approved-class allowlist. Returns
    (decision, reason)."""
    payload = payload or {}
    if mode == "bypass":
        return "ALLOW", "bypass"

    level, why = _static_risk(job_type, payload)

    if mode == "auto":
        if level == "stop":
            return "DENY", why or "matched STOP pattern"
        if level == "ask":
            return "CONFIRM", why or "matched ASK pattern"
        return "ALLOW", "static check clean"

    # mode == "default"
    key = _job_class_key(job_type, payload)
    if _is_class_approved(key):
        # CRITICAL SAFETY MITIGATION: an approved class only auto-ALLOWs when THIS
        # payload ALSO passes the static risk check right now -- never silently
        # auto-run a destructive command just because its class was approved once.
        if level == "clean":
            return "ALLOW", "class previously approved (%s) and payload clean" % key
        return "CONFIRM", "class approved but this payload is risky (%s): %s" % (level, why)
    return "CONFIRM", "first-seen class (%s) requires approval" % key


def _gate_token_for_class(key):
    """Derive a stable gate token from the job CLASS key (not the job id), so one human
    approval unblocks every queued same-class job sitting in awaiting/, mirroring
    tools/contract_gate.py's _stable_token(op_class, detail) pattern."""
    h = hashlib.sha256(("task_router_job_class::%s" % key).encode("utf-8")).hexdigest()[:16]
    return "gate_%s" % h


def _write_job_gate(token, question, context):
    """Write a HITL gate file DIRECTLY -- same shape and directory as
    tools/contract_gate.py's _create_gate() / tools/gate_ops.py's GATE_DIR
    (ALLOWED_BASE/.companion_gates/<token>.json with fields {token, question, context,
    asked_at, answered, answer}) -- so relay/fleet_runner.py:_pending_gates() (which scans
    that directory) and the cockpit's Approve/Deny UI pick it up automatically. No cockpit
    changes needed.

    Deliberately does NOT call tools/gate_ops.gate_ask(): that function calls
    require_unlocked(), which DENIES outside an HTTP request context, and task_router runs
    as a standalone process (no HTTP request in flight)."""
    if ALLOWED_BASE is None:
        return
    try:
        gate_dir = ALLOWED_BASE / ".companion_gates"
        gate_dir.mkdir(parents=True, exist_ok=True)
        gate_file = gate_dir / ("%s.json" % token)
        if gate_file.is_file():
            return  # already posted -- don't clobber a gate that may already be answered
        payload = {
            "token": token,
            "question": question,
            "context": context,
            "asked_at": time.time(),
            "answered": False,
            "answer": None,
        }
        gate_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            notify_approval_gate("ジョブ承認が必要です / Job approval needed", question[:180], gate_file)
        except Exception:
            pass
    except Exception:
        pass


def _read_job_gate(token):
    """Read a gate file written by _write_job_gate(). Returns dict or None (missing/bad)."""
    if ALLOWED_BASE is None:
        return None
    try:
        gate_file = ALLOWED_BASE / ".companion_gates" / ("%s.json" % token)
        if not gate_file.is_file():
            return None
        return json.loads(gate_file.read_text(encoding="utf-8"))
    except Exception:
        return None


#: Where the fleet keeps its live state. The same directory code_task.py reads, deliberately:
#: two answers to "is a fleet running" that can disagree is worse than either alone.
#:
#: READ FROM THE ENVIRONMENT, because the repository already redirects exactly this. conftest
#: points FLEET_STATE_DIR at a temp directory for every test run, and relay.project_memory
#: resolves its own paths through the same variable. Hardcoding .fleet here would have made a
#: test that reaches fleet_handoff() append add_goal to the OPERATOR'S commands.json -- which
#: a running fleet reads and acts on, so not a dirtied record but a goal nobody asked for
#: handed to a live run. Honouring the variable costs nothing and is already the convention.
FLEET_STATE_DIR = os.environ.get("FLEET_STATE_DIR", "").strip() or os.path.join(REPO, ".fleet")

#: A status.json older than this is not a live run, whatever it says inside. A fleet that died
#: leaves its last snapshot behind, and a stale file claiming running=True would have every
#: later goal queued into a run that ended hours ago.
FLEET_LIVE_MAX_AGE_S = 30

#: Spawning a fleet starts a browser, a set of workers and spends the tenant's Copilot budget.
#: Doing that because a sentence arrived over a tunnel is a bigger act than queueing one, so it
#: is opt-in. Off, a goal that arrives with no run in flight waits and says it is waiting.
AUTOSTART = os.environ.get("FLEET_INTAKE_AUTOSTART", "0") == "1"


def fleet_is_live(state_dir=None) -> bool:
    """Whether a fleet run is in flight right now.

    Read the same way code_task.py reads it -- file age AND the running flag -- because this
    decides whether a goal joins the current run or waits, and the failure it prevents is
    already on the record: two fleets share the one dedicated Edge and clobber each other's
    status.json, and the second run's work showed up as a phantom worker behind the first.
    """
    sd = state_dir or FLEET_STATE_DIR
    try:
        sp = os.path.join(sd, "status.json")
        if not os.path.isfile(sp) or (time.time() - os.path.getmtime(sp)) > FLEET_LIVE_MAX_AGE_S:
            return False
        with open(sp, encoding="utf-8-sig") as fh:
            return bool(json.load(fh).get("running"))
    except Exception:
        return False


#: One file per command, under <state_dir>/commands.d/. See write_command.
COMMANDS_DIR = "commands.d"

#: Strictly increasing within this process, so two commands written inside one clock tick still
#: sort in the order they were sent. See write_command for why the clock alone cannot do it.
_SEQ = itertools.count()
_SEQ_LOCK = threading.Lock()


def _next_seq() -> int:
    with _SEQ_LOCK:
        return next(_SEQ)


def write_command(state_dir, patch: dict) -> str:
    """Leave one command for the running fleet. Returns the path written.

    A FILE OF ITS OWN, WHICH IS WHY THERE IS NO LOCK. Every writer used to read the whole
    commands.json, add its entry and write it back, so whichever replaced second silently
    deleted the other's -- and a lost goal looks exactly like a goal that was never sent. A
    lock fixed that for the Python writers and could not fix it for ui/CopilotChat.cs, which
    writes the same file from a separately built binary and takes no lock; the reader took
    none either. Giving each command its own uniquely named file removes the read-modify-write
    that made a lock necessary: nothing merges, so nothing can clobber.

    The name is time_ns, then a per-process sequence number, then a random tail.

    ALL THREE ARE LOAD-BEARING, and the middle one was missing until a test caught it. The
    reader takes these in filename order, so the name has to carry the order they were sent in
    -- and time_ns cannot: Windows advances the clock in ~15.6 ms steps, so three goals sent in
    a row read the SAME nanosecond and the random tail decided their order. Measured: 0, 1, 2
    went in and 0, 2, 1 came out. The counter makes one process's writes strictly ordered
    whatever the clock does. Two different processes writing inside one tick still order
    arbitrarily between themselves, which is the truth about concurrent senders rather than a
    gap: nothing here can know which of them meant to go first.

    The random tail stays because a clock is not a counter across processes either -- without
    it, two processes in the same tick with the same sequence number would collide, and here a
    collision means one command silently overwrites another. That is the defect this repo just
    fixed in new_experiment_id.

    Written to .tmp in the same directory and renamed, so a reader never sees half a command;
    the reader skips .tmp for the same reason. The rename is retried briefly because on Windows
    a rename onto a path someone has open fails with PermissionError.
    """
    d = os.path.join(state_dir, COMMANDS_DIR)
    os.makedirs(d, exist_ok=True)
    name = "%019d-%09d-%s" % (time.time_ns(), _next_seq(), uuid.uuid4().hex[:8])
    path = os.path.join(d, name + ".json")
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        json.dump(patch, fh, ensure_ascii=False)
    until = time.time() + 2.0
    while True:
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            if time.time() > until:
                raise
            time.sleep(0.02)


def add_goal_to_live_fleet(goal: str, state_dir=None, priority: bool = False,
                           entry: dict = None) -> None:
    """Append a goal to the running fleet's command channel.

    utf-8 with no BOM on the way out and utf-8-sig on the way in, matching code_task.py: the
    fleet reads both, and writing what the other writer writes is how the two stay compatible.

    `entry` lets a caller supply the whole command dict -- code_task adds `cwd` and `checks` --
    so it can share this path instead of keeping its own copy of it.
    """
    sd = state_dir or FLEET_STATE_DIR
    item = dict(entry) if entry else {"text": goal, "priority": bool(priority)}
    write_command(sd, {"add_goal": [item]})


def fleet_handoff(goal: str, jid: str, state_dir=None):
    """Deliver a fleet-bound goal. Returns the (status, result) the job record should carry."""
    if not (goal or "").strip():
        return "error", {"handoff": "for_fleet/%s.txt" % jid, "detail": "empty goal"}
    if fleet_is_live(state_dir):
        add_goal_to_live_fleet(goal, state_dir)
        return "dispatched", {"handoff": "for_fleet/%s.txt" % jid,
                              "delivered": "add_goal", "note": "queued into the running fleet"}
    if AUTOSTART:
        return "awaiting_fleet", {"handoff": "for_fleet/%s.txt" % jid,
                                  "note": "autostart is on but is not implemented here; the "
                                          "goal is recorded and waiting"}
    # SAYS IT IS WAITING, rather than "dispatched". The old wording claimed delivery for a
    # file nobody read, and a status that overstates what happened is how a queue goes
    # unnoticed for months.
    return "awaiting_fleet", {"handoff": "for_fleet/%s.txt" % jid,
                              "note": "no fleet run is in flight; the goal waits for one"}


def run_job(job, now_ts=None):
    """Execute (LOCAL) or hand off (FLEET/CLAUDE) a single job. Returns the done-record dict.
    Never raises -- any failure is captured as status 'error'."""
    jid = job.get("id", "noid")
    dest = destination_for(job)
    rec = {"id": jid, "type": job.get("type"), "destination": dest,
           "ts_done": now_ts, "status": None, "result": None, "error": None}
    # PROVENANCE SURVIVES INTO THE ARCHIVE. fleet_intake records where an instruction came
    # from -- {"via": "mcp", "source": ...} -- so that a goal handed in over the tunnel can be
    # told apart from one an operator typed. It was being dropped here, at the moment the job
    # became a record: the done/ file read origin=None for the first real submission, and the
    # only place the difference could still be seen was the pending file that had just been
    # deleted. A distinction that does not reach the audit trail is not a distinction.
    if job.get("origin"):
        rec["origin"] = job["origin"]
    try:
        if dest == "local":
            job_type = job.get("type")
            fn = LOCAL_EXECUTORS.get(job_type)
            if not fn:
                rec["status"], rec["error"] = "error", "no local executor for type %r" % job_type
            else:
                payload = dict(job.get("payload") or {}, id=jid)
                # ── approval gate chokepoint: this is where TASK_JOB_APPROVAL_MODE bites,
                # immediately before fn(payload) would otherwise run unconditionally ──
                # Read the cockpit's persistent choice live. This lets an operator switch
                # confirmation/auto/bypass without restarting a long-running router.
                decision, reason = job_gate(
                    job_type, payload, _current_approval_mode(TASK_JOB_APPROVAL_MODE)
                )
                if decision == "ALLOW":
                    rec["status"], rec["result"], rec["error"] = fn(payload)
                elif decision == "DENY":
                    rec["status"], rec["error"] = "denied", reason
                else:  # CONFIRM -- hold the job, raise a desktop gate, do NOT block the loop
                    key = _job_class_key(job_type, payload)
                    token = _gate_token_for_class(key)
                    detail = (payload.get("cmd") or payload.get("command") or
                              payload.get("code") or payload.get("path") or "")
                    question = ("ジョブ承認: %s %s ? "
                                "(このクラスを許可すると次回以降自動実行) / "
                                "Approve job class %r ?" % (job_type, str(detail)[:160], key))
                    _write_job_gate(token, question, "task_router job class: %s" % key)
                    rec["status"] = "awaiting_approval"
                    rec["result"] = {"gate_token": token, "class_key": key}
                    rec["error"] = reason
        elif dest == "fleet":
            # A HANDOFF NOBODY COLLECTED. This branch wrote for_fleet/<id>.txt, marked the job
            # "dispatched" and stopped -- and no file in relay/, bridge/, tools/, ui/ or
            # scripts/ ever read that directory. Every fleet-bound job this router has ever
            # seen was filed as delivered and went nowhere. The file is still written, because
            # it is the record of what was asked for, but the delivery now actually happens.
            #
            # AND "awaiting_fleet" HAD THE SAME PROBLEM ONE LAYER DOWN. A goal that arrived
            # while no fleet was running was written here, filed in done/ as awaiting_fleet,
            # and that was the end of it: nothing re-tried it when a fleet later started, so
            # it was not awaiting anything. Six of them had accumulated saying so.
            #
            # So for_fleet/ now means ONE thing -- goals still waiting for a fleet. A goal
            # that was delivered leaves no file, because its done/ record already says
            # "dispatched" and names how; a goal that was not leaves one, and every drain pass
            # tries the waiting ones again while a fleet is live.
            goal = (job.get("payload") or {}).get("goal") or (job.get("payload") or {}).get("text", "")
            rec["status"], rec["result"] = fleet_handoff(goal, jid)
            if rec["status"] != "dispatched":
                with open(_p("for_fleet", "%s.txt" % jid), "w", encoding="utf-8") as f:
                    f.write(goal)
        else:  # claude
            with open(_p("for_claude", "%s.json" % jid), "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=2)
            rec["status"], rec["result"] = "escalated", {"handoff": "for_claude/%s.json" % jid}
    except subprocess.TimeoutExpired:
        rec["status"], rec["error"] = "error", "timeout after %ds" % LOCAL_TIMEOUT_S
    except Exception as e:
        rec["status"], rec["error"] = "error", "%s: %s" % (type(e).__name__, e)
    return rec


def dispatch_once(now_ts=None):
    """Claim and process every pending job, then re-check jobs already held in awaiting/
    for a gate answer. Returns the list of done/awaiting-records produced. Non-blocking:
    a CONFIRM decision moves the job to awaiting/ instead of sleeping for a human click,
    so this call always returns promptly regardless of approval-gate state."""
    ensure_dirs()
    out = []
    for name in sorted(os.listdir(_p("pending", ""))):
        if not name.endswith(".json"):
            continue
        src = _p("pending", name)
        claimed = _p("running", name)
        try:
            os.replace(src, claimed)   # atomic claim; if another router grabbed it, this raises
        except OSError:
            continue
        try:
            with open(claimed, encoding="utf-8") as f:
                job = json.load(f)
        except Exception as e:
            job = {"id": name[:-5], "type": None, "payload": {}, "_parse_error": str(e)}
        rec = run_job(job, now_ts=now_ts)
        if rec.get("status") == "awaiting_approval":
            # non-blocking: move the claimed job (unchanged) into awaiting/ so a later
            # poll can pick it back up once the human answers the gate. No done/ write,
            # no sleep -- the loop keeps servicing the rest of the pending queue.
            try:
                os.replace(claimed, _p("awaiting", name))
            except OSError:
                pass  # best-effort; job stays claimed in running/ rather than being lost
        else:
            with open(_p("done", name), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            os.remove(claimed)
        out.append(rec)
    out.extend(_recheck_awaiting(now_ts=now_ts))
    out.extend(_deliver_waiting_goals(now_ts=now_ts))
    return out


def _deliver_waiting_goals(now_ts=None, state_dir=None):
    """Hand the fleet the goals that arrived while it was not running.

    THE GAP THIS CLOSES. A fleet-bound goal submitted with no run in flight was filed as
    "awaiting_fleet" and written to done/, which is terminal -- nothing looked at it again.
    The status was the only thing waiting. Six such records had built up, each naming a goal
    that would never be delivered however long a fleet ran afterwards.

    Runs on every drain pass. With no fleet in flight it does nothing and costs one status
    read, which is the same check fleet_handoff already makes.
    """
    out = []
    ensure_dirs()
    if not fleet_is_live(state_dir):
        return out
    try:
        names = sorted(os.listdir(_p("for_fleet", "")))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".txt"):
            continue
        path = _p("for_fleet", name)
        try:
            with open(path, encoding="utf-8") as fh:
                goal = fh.read()
        except OSError:
            continue
        jid = name[:-4]
        if not (goal or "").strip():
            # Nothing to deliver and nothing to keep waiting for. Removing it is better than
            # retrying an empty goal on every pass for the life of the machine.
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        status, result = fleet_handoff(goal, jid, state_dir)
        if status != "dispatched":
            continue                       # still no run; leave it waiting
        # DELETED ONLY AFTER DELIVERY SUCCEEDS. Removing it first would lose the goal if the
        # write to the fleet failed, and a lost goal looks exactly like one never sent.
        try:
            os.remove(path)
        except OSError:
            pass
        rec = {"id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": now_ts,
               "status": status, "result": dict(result or {}, delivered_late=True),
               "error": None}
        try:
            with open(_p("done", "%s.delivered.json" % jid), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        out.append(rec)
    return out


def _recheck_awaiting(now_ts=None):
    """Re-scan awaiting/ for jobs whose gate has since been answered by a human (the
    cockpit, or anything writing the same gate-file shape). Never sleeps/blocks --
    unanswered gates are simply left in place for the next tick.

    approved  -> record the class in the approved_jobs.json allowlist, actually run the
                 job (this is the ONLY place a CONFIRM-ed job executes), move to done/.
    denied    -> write done/ with status="denied" (never executed).
    unanswered -> leave the job in awaiting/ untouched.
    """
    out = []
    ensure_dirs()
    try:
        names = sorted(os.listdir(_p("awaiting", "")))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = _p("awaiting", name)
        try:
            with open(path, encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue  # unreadable -- leave it for manual inspection rather than losing it
        jid = job.get("id", name[:-5] if name.endswith(".json") else name)
        job_type = job.get("type")
        payload = dict(job.get("payload") or {}, id=jid)
        key = _job_class_key(job_type, payload)
        token = _gate_token_for_class(key)
        gate = _read_job_gate(token)
        if gate is None or not gate.get("answered"):
            continue  # still waiting -- no sleep, just move on to the next tick
        answer = str(gate.get("answer") or "").lower().strip()
        rec = {"id": jid, "type": job_type, "destination": "local",
               "ts_done": now_ts, "status": None, "result": None, "error": None}
        try:
            if answer == "approved":
                _approve_class(key, example=json.dumps(payload, ensure_ascii=False)[:200])
                fn = LOCAL_EXECUTORS.get(job_type)
                if not fn:
                    rec["status"], rec["error"] = "error", "no local executor for type %r" % job_type
                else:
                    rec["status"], rec["result"], rec["error"] = fn(payload)
            else:
                rec["status"], rec["error"] = "denied", "gate answered: %r" % answer
        except subprocess.TimeoutExpired:
            rec["status"], rec["error"] = "error", "timeout after %ds" % LOCAL_TIMEOUT_S
        except Exception as e:
            rec["status"], rec["error"] = "error", "%s: %s" % (type(e).__name__, e)
        try:
            with open(_p("done", name), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            os.remove(path)
        except OSError:
            pass
        out.append(rec)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Typed-job task router (LOCAL/FLEET/CLAUDE).")
    ap.add_argument("--once", action="store_true", help="process the pending queue once and exit")
    ap.add_argument("--poll-s", type=float, default=2.0, help="poll interval when looping")
    args = ap.parse_args()
    ensure_dirs()
    if args.once:
        recs = dispatch_once()
        print(json.dumps(recs, ensure_ascii=False))
        return
    print("task_router: polling %s every %ss" % (_p("pending", ""), args.poll_s))
    while True:
        for rec in dispatch_once():
            print("[task] %s/%s -> %s/%s" % (rec.get("type"), rec.get("id"),
                                             rec.get("destination"), rec.get("status")))
        time.sleep(args.poll_s)


if __name__ == "__main__":
    main()
