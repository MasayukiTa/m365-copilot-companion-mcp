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

  AND ONCE A FLEET GOAL LANDED, NOTHING EVER LOOKED AT THE JOB AGAIN EITHER -- until
  2026-09-15. "dispatched" was written the instant the goal left the queue and never revisited:
  a goal that finished cleanly ten minutes later and one that was still running when someone
  stopped the fleet both read exactly "dispatched" in done/<jid>.json, indefinitely, because
  the outcome existed only in relay/relay_fleet.py's own per-worker ledger
  (<state_dir>/socket_route.jsonl) and nothing joined the two. `job_status(jid)` (below) is the
  read path that closes this: it answers "did this job finish, and how" by checking, in order,
  a reconciled outcome record, a live scan of that ledger, whether the jid is a worker the
  CURRENT run is still holding, and only then falling back to an honest "unknown" rather than
  repeating "dispatched" forever. `_reconcile_outcomes()` is the write path that keeps
  done/<jid>.outcome.json current on every dispatch tick so a caller does not have to run the
  read path's live-scan fallback to get an up-to-date answer.

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
import re
import subprocess
import sys
import threading
import uuid
import time
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE OPERATOR'S .env, WHICH THIS MODULE HAD NEVER READ. Every setting below comes from
# os.environ, and this module runs as a fresh subprocess: the supervisor launches
# `python relay/task_router.py --once` each pass, and that process inherits an environment
# nobody put .env into. Measured: a fresh subprocess reported AUTOSTART False and no agent URL
# while both were configured in .env. So FLEET_INTAKE_AUTOSTART could be set to 1 and change
# nothing, which is the third time in one night that a flag turned out not to reach the branch
# it names -- MCP_TOOL_MAP_INCLUDE was silently dropped by the tool cap, the autostart branch
# itself was an empty stub, and now the flag that would have enabled it.
#
# override=False, so an explicitly exported variable still wins: conftest points FLEET_STATE_DIR
# at a temp directory for every test run, and a file on disk must not be able to take that back.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(os.path.join(REPO, ".env"), override=False)
except Exception:
    pass

TASKS = os.path.join(REPO, ".fleet", "tasks")
SUBDIRS = ("pending", "running", "done", "awaiting", "awaiting_ack", "for_fleet", "for_claude")
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
#              A "class" is only as wide as a payload's text cannot carry code: python code and
#              interpreter / shell-operator commands are keyed by their exact normalised text,
#              so approving one approves that text only (see _job_class_key).
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

from tools import childproc

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
    from relay import splittability as _splittability
except Exception:
    _splittability = None

try:
    from relay.autonomy_gate import _STOP_PATTERNS, _ASK_PATTERNS, _matches as _autonomy_matches
except Exception:
    _STOP_PATTERNS = ()
    _ASK_PATTERNS = ()

    def _autonomy_matches(_text, _patterns):
        return []

# RESOLVED ON FIRST USE, NOT AT IMPORT. `tools.file_ops` imports `tools.security`, which imports
# fastmcp and the server's auth stack -- and this module is run by the supervisor as
# `task_router.py --once` every tick, usually to find an empty queue. Measured 2026-09-24: the
# import alone took 3-12 s and ~89 MB per tick, and it was the largest single part of the ~26 s a
# submitted job waited before anything picked it up. The two names stay module attributes because
# tests set `task_router.ALLOWED_BASE` directly; `_UNRESOLVED` means "ask file_ops when needed".
#
# NOT FIXED IN tools/security.py, although that is where the heavy import lives: that file is in
# the self-improvement frozen set, and a latency fix is not a reason to spend a re-sign.
_UNRESOLVED = object()
_validate_path = _UNRESOLVED
ALLOWED_BASE = _UNRESOLVED


def _file_ops_names():
    """(validate_path, allowed_base) -- this module's attributes if set, else tools.file_ops'.

    Same failure behaviour as the import it replaces: if file_ops cannot be imported, both are
    None and the callers' existing `is None` branches take over."""
    global _validate_path, ALLOWED_BASE
    if _validate_path is _UNRESOLVED or ALLOWED_BASE is _UNRESOLVED:
        try:
            from tools.file_ops import _validate_path as _vp, ALLOWED_BASE as _ab
        except Exception:
            _vp, _ab = None, None
        if _validate_path is _UNRESOLVED:
            _validate_path = _vp
        if ALLOWED_BASE is _UNRESOLVED:
            ALLOWED_BASE = _ab
    return _validate_path, ALLOWED_BASE

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
                       errors="replace", timeout=LOCAL_TIMEOUT_S, cwd=REPO,
                       creationflags=childproc.headless_creationflags())
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return ("ok" if r.returncode == 0 else "error"), {"rc": r.returncode, "output": out[:20000]}, None


def _exec_python(payload):
    code = payload.get("code")
    if not code:
        return "error", None, "python job missing 'code'"
    py = VENVPY if os.path.isfile(VENVPY) else sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                       errors="replace", timeout=LOCAL_TIMEOUT_S, cwd=REPO, env=env,
                       creationflags=childproc.headless_creationflags())
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
                       timeout=LOCAL_TIMEOUT_S,
                       creationflags=childproc.headless_creationflags())
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
    validate_path, _ = _file_ops_names()
    try:
        if validate_path is not None:
            resolved = str(validate_path(path))
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

#: Programs that run code or a command taken from their OWN ARGUMENTS. For these, the first two
#: tokens name no action at all: `python -c`, `powershell -Command`, `cmd /c`, `bash -c`,
#: `node -e`, `wsl <anything>` each describe "whatever text follows", so a class built from them
#: is a class of every program. Missing an entry here fails toward the weaker class key, so this
#: is the list to extend when a new launcher turns up.
_CODE_RUNNERS = frozenset((
    "python", "python3", "py", "pyw", "pythonw", "pypy", "pypy3", "ipython",
    "powershell", "powershell_ise", "pwsh", "cmd", "command", "conhost",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "wsl", "busybox",
    "node", "nodejs", "deno", "bun", "npx", "perl", "ruby", "php", "lua", "tclsh", "osascript",
    "cscript", "wscript", "mshta", "rundll32", "regsvr32", "msiexec", "installutil", "msbuild",
    "start", "call", "env", "xargs", "find", "forfiles", "wmic", "schtasks", "at", "runas",
    "sudo", "nohup", "iex", "invoke-expression", "awk", "gawk", "sed", "uv", "uvx", "pipx",
))

#: Argument tokens that hand the NEXT text to something that executes it, whatever the program:
#: `git -c core.pager=..`, `find -exec`, `git rebase -x`, `git fetch --upload-pack=..`,
#: `git submodule foreach`, `docker run`, `npm exec`. Compared case-insensitively, before any `=`.
_CODE_CARRYING_ARGS = frozenset((
    "-c", "/c", "/k", "/r", "-e", "-x", "--eval", "-command", "--command", "-encodedcommand",
    "-enc", "-ec", "-exec", "--exec", "-execdir", "-ok", "--upload-pack", "--receive-pack",
    "--config", "exec", "run", "foreach", "eval", "call", "start", "invoke-expression", "iex",
))

#: A token a class may be built from: letters, digits and path/option punctuation only. No
#: quote (a quoted program path hides where the program name ends), no shell operator
#: (& | ; < > ^), no expansion (% ! $ `), no grouping or glob. Anything else and the text can
#: say more than its first two tokens do.
_PLAIN_TOKEN = re.compile(r"^[\w.:\\/@+,=~-]+$")


def _normalised_payload_text(text):
    """The text an exact approval is keyed on: line endings unified, outer whitespace dropped.
    Nothing inside is collapsed -- in Python indentation and string contents are code, so two
    texts that differ inside are two different payloads."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _payload_text(job_type, payload):
    """What the operator is shown, and what an exact approval covers, for one job."""
    payload = payload or {}
    if job_type == "shell":
        return str(payload.get("cmd") or payload.get("command") or "")
    if job_type == "python":
        return str(payload.get("code") or "")
    return json.dumps({k: v for k, v in payload.items() if k != "id"},
                      ensure_ascii=False, sort_keys=True, default=str)


def _exact_key(job_type, payload):
    """Approval key for exactly this payload and nothing else.

    `@sha256:` rather than `::` so no class key can ever spell one: a class key is always
    `<type>::<tokens>`, and a command whose first token happened to read `exact::<hex>` must not
    be able to stand in for the payload that hex was computed from."""
    text = _normalised_payload_text(_payload_text(job_type, payload))
    return "%s@sha256:%s" % (job_type, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _program_name(token):
    """`C:\\Python\\python.exe` -> `python`. Lower-cased, directory and executable suffix dropped."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".com", ".bat", ".cmd", ".ps1"):
        if name.endswith(ext):
            name = name[:-len(ext)]
            break
    return name


def _shell_class_prefix(cmd):
    """The first-two-token class this command may share an approval with -- or None when its
    own text can carry code, so that only an approval of this exact text may cover it.

    None when: the command spans lines; any token is not plain (see _PLAIN_TOKEN); the program
    runs code from its arguments (_CODE_RUNNERS, including `python3.12`-style names); the
    second token is an option rather than a subcommand, so the pair names no action
    (`git -c`, `node -e`); or any token is a code-carrying argument (_CODE_CARRYING_ARGS)."""
    text = _normalised_payload_text(cmd)
    if not text or "\n" in text:
        return None
    tokens = text.split()
    if not all(_PLAIN_TOKEN.match(t) for t in tokens):
        return None
    prog = _program_name(tokens[0])
    if prog in _CODE_RUNNERS or re.match(r"^(python|pypy)[\d.]*w?$", prog):
        return None
    if len(tokens) > 1 and tokens[1][:1] in ("-", "/"):
        return None
    if any(t.split("=", 1)[0].lower() in _CODE_CARRYING_ARGS for t in tokens[1:]):
        return None
    return " ".join(tokens[:2])


def _job_class_key(job_type, payload):
    """Pure fn: map (job_type, payload) -> a stable class-key string for the approval
    allowlist. Deliberately granular: "git status" and "git push --force" are DISTINCT
    classes -- collapsing to just the program name (e.g. "git") would let one approval of
    a benign invocation silently cover a destructive one later. file keys on
    (op, resolved parent dir). Hermetically testable: no I/O besides path resolution.

    THE SAME REASONING, CARRIED ONE STEP FURTHER. shell and python used to key on the first two
    whitespace tokens of the command/code, which for an interpreter is no class at all:
    `python -c "print(1)"` and `python -c "<anything>"` share `shell::python -c`, and a python
    job's own code keyed on its first two tokens (`import os`), so one approval covered every
    program that began the same way -- with only the regex static check between an obfuscated
    payload and execution, and no further human decision (SEC-07).

    So a key now only groups payloads whose differences cannot be code:
      python -- always the exact normalised code (_exact_key). The payload IS code.
      shell  -- the first-two-token class only when _shell_class_prefix() finds the command
                cannot carry code past that prefix; otherwise the exact normalised command."""
    payload = payload or {}
    if job_type == "shell":
        prefix = _shell_class_prefix(payload.get("cmd") or payload.get("command") or "")
        if prefix is None:
            return _exact_key(job_type, payload)
        return "shell::%s" % prefix
    if job_type == "python":
        return _exact_key(job_type, payload)
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
        try:
            from tools.approval_policy import record_bypass_decision
            record_bypass_decision(
                "task_router.job_gate",
                _job_gate_question(job_type, payload, _job_class_key(job_type, payload)),
                "ALLOW (bypass); job_type=%s" % job_type)
        except Exception:
            pass
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


def _gate_key(job_type, payload, level):
    """The key a CONFIRM gate is posted under -- which is exactly what answering it approves.

    A clean payload asks about its class key (for code-carrying forms that already IS the exact
    payload). A payload the static check flagged asks about THAT payload only.

    WHY THE SECOND HALF. Gate files are durable and the token was derived from the class key
    alone, so once any `git log ...` gate had been answered "approved", a later same-class
    payload that job_gate() held back as risky ("class approved but this payload is risky")
    was moved to awaiting/, found the old answered gate under the same token, and ran on the
    next recheck -- the confirm existed, nobody was asked. The same replay reached `auto`
    mode's ASK-pattern confirms. A flagged payload's gate is now its own."""
    if level == "clean":
        return _job_class_key(job_type, payload)
    return _exact_key(job_type, payload)


def _job_gate_question(job_type, payload, gate_key):
    """The approval text: says what answering it covers, and shows the payload IN FULL.

    It used to show the first 160 characters -- the tail of an obfuscated payload is exactly
    what would sit past that cut -- and to call every approval a class approval even when the
    class was every program an interpreter could run."""
    text = _normalised_payload_text(_payload_text(job_type, payload))
    if "@sha256:" in gate_key:
        return ("ジョブ承認（この内容のみ）: %s\n"
                "承認されるのは以下の内容そのものだけです。1文字でも違えば再度確認します。\n"
                "Approve exactly this %s job? Only this exact text is approved; any change "
                "asks again.\n"
                "----\n%s\n----\n%s" % (job_type, job_type, text, gate_key))
    return ("ジョブ承認（クラス）: %s\n"
            "承認すると、クラス %r に属する以後のジョブは静的検査が clean な限り確認なしで"
            "実行されます。\n"
            "Approve job class %r ? Later jobs in this class run without asking while the "
            "static check finds them clean.\n"
            "----\n%s\n----" % (job_type, gate_key, gate_key, text))


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
    _, allowed_base = _file_ops_names()
    if allowed_base is None:
        return
    try:
        gate_dir = allowed_base / ".companion_gates"
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
    _, allowed_base = _file_ops_names()
    if allowed_base is None:
        return None
    try:
        gate_file = allowed_base / ".companion_gates" / ("%s.json" % token)
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

    A STALE status.json IS NOT PROOF THE PROCESS DIED. `fleet_runner.on_tick` writes
    status.json inside `try: ... except Exception: pass` (fleet_runner.py), so a write that
    fails -- e.g. `_write_atomic`'s PermissionError against the cockpit's own reader losing its
    retry race, or the disk floor being exhausted by a long, heavy run -- is swallowed silently
    and the sweep loop keeps going with a live browser and live workers. Measured 2026-09-25:
    a run that had grown to 41 workers over free RAM 2151 MB / free disk 0.6 GB went stale by
    this reading while its process (and Edge window) were still very much alive, and
    autostart's "a fleet is already running" gate read the stale file as "not live" and started
    a SECOND fleet_runner on top of it -- two coordinator processes, two Edge windows, the
    second one's workers counted as refusals by the first one's rate budget.
    Before giving up on a stale/missing status.json, fall back to the OS-level signal:
    fleet_runner writes `fleet_run_active.json` once at startup (pid/start_ts/argv) and clears
    it on a clean exit (see fleet_runner._write_active_marker / ACTIVE_MARKER). If that marker
    names a pid that is still running, the fleet is live even though its status snapshot is not
    advancing -- join it, do not start a second one.
    """
    sd = state_dir or FLEET_STATE_DIR
    try:
        sp = os.path.join(sd, "status.json")
        if os.path.isfile(sp) and (time.time() - os.path.getmtime(sp)) <= FLEET_LIVE_MAX_AGE_S:
            with open(sp, encoding="utf-8-sig") as fh:
                return bool(json.load(fh).get("running"))
    except Exception:
        pass
    return _active_marker_process_alive(sd)


def _active_marker_process_alive(state_dir) -> bool:
    """Fallback liveness read for a stale/unreadable status.json: is the pid recorded in
    fleet_runner's `fleet_run_active.json` still running? Never raises; a missing/corrupt
    marker or an unreadable pid answers False, same as "no fleet" did before this fallback
    existed -- this only ADDS a way to say True, it never removes the old one.
    """
    try:
        marker_path = os.path.join(state_dir, "fleet_run_active.json")
        with open(marker_path, encoding="utf-8-sig") as fh:
            rec = json.load(fh)
        pid = rec.get("pid")
        if pid is None:
            return False
        return _pid_alive(pid)
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


#: Where the fleet drops a receipt when it actually READS a command. One file per goal id,
#: written by relay/fleet_runner.read_commands the instant before it deletes the command. Its
#: presence is the only proof on the receiving side that a dispatched goal was picked up rather
#: than lost into the stale-status window (see fleet_landing_confirmed and the probe note).
ACKS_DIR = "acks"


def _ack_path(jid: str, state_dir=None) -> str:
    sd = state_dir or FLEET_STATE_DIR
    return os.path.join(sd, ACKS_DIR, "%s.ack" % jid)


def fleet_landing_confirmed(jid: str, state_dir=None) -> bool:
    """Whether the running fleet has actually read the goal handed over as `jid`.

    dispatched IS NOT DELIVERED. fleet_handoff writes a done/ record saying "dispatched" the
    moment fleet_is_live() is True, but that check trusts a status.json up to
    FLEET_LIVE_MAX_AGE_S old -- so a run that had already died still read as live for up to
    30s, and a goal queued in that window went into commands.d/ that no one would ever read.
    read_commands now drops an ack receipt as it consumes a command; this reports whether that
    receipt exists, which is what turns "we said we delivered it" into "the fleet took it".
    """
    try:
        return os.path.isfile(_ack_path(jid, state_dir))
    except OSError:
        return False


def read_ack_receipt(jid: str, state_dir=None):
    """The parsed receipt body fleet_runner.read_commands wrote when it took `jid`'s command,
    or None when there is none yet (not landed) or it cannot be read.

    A SEPARATE QUESTION FROM fleet_landing_confirmed's PLAIN EXISTENCE CHECK (e822fb6 gap #1).
    That commit made _apply_command validate every command before touching anything and, when
    a command is refused, write the SAME receipt file anyway -- {"read": True, "rejected": True,
    "errors": [...]} -- because the fleet still genuinely READ it off the channel; only applying
    it was refused (see fleet_runner.read_commands' own comment on this). A caller that only
    checks "does the ack file exist" cannot tell that apart from a command that was read AND
    applied, and task_router._reconcile_landings did exactly that: it turned a rejected
    add_goal into a done/ record saying "dispatched", "landing_confirmed": True -- the operator
    told a goal had landed for a goal the fleet never queued at all. This is the read that lets
    a caller check `rejected` before believing "landed" means "queued".
    """
    try:
        with open(_ack_path(jid, state_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def add_goal_to_live_fleet(goal: str, state_dir=None, priority: bool = False,
                           entry: dict = None, jid: str = None) -> None:
    """Append a goal to the running fleet's command channel.

    utf-8 with no BOM on the way out and utf-8-sig on the way in, matching code_task.py: the
    fleet reads both, and writing what the other writer writes is how the two stay compatible.

    `entry` lets a caller supply the whole command dict -- code_task adds `cwd` and `checks` --
    so it can share this path instead of keeping its own copy of it.

    `jid`, when given, adds an `ack` path to the COMMAND (so the fleet leaves a receipt when
    it reads it) AND an `jid` key on the ITEM itself, so this admission-time id survives into
    the worker goals_from_command builds and, from there, into every snapshot row and
    history.json entry that worker ever produces. Without the second half, `jid` never
    reached the item goals_from_command reads, and nothing downstream of admission could join
    back to `.fleet/tasks/done/<jid>.json` -- the codex-plan item-1 gap (2026-09-09): run_id
    identifies a fleet SWEEP, jid identifies a GOAL, and until now no field carried the
    latter past this function. `add_goal` readers ignore keys they do not recognise, so
    adding this one is safe for every existing consumer that predates it.
    """
    sd = state_dir or FLEET_STATE_DIR
    item = dict(entry) if entry else {"text": goal, "priority": bool(priority)}
    patch = {"add_goal": [item]}
    if jid:
        patch["ack"] = _ack_path(jid, sd)
        item["jid"] = jid
    write_command(sd, patch)


#: Where an autostart attempt is recorded, beside the run's own state. Kept so the NEXT pass
#: can tell "a fleet is coming up" from "the last attempt died", which are the two situations
#: that must not both lead to launching again.
AUTOSTART_STATE = "autostart.json"

#: How long a launched run may take to publish status.json before the attempt counts as failed.
#: A cold start opens a browser and signs in; generous, because launching a second fleet is far
#: worse than waiting. Two runs share one dedicated Edge and clobber each other's status.json.
AUTOSTART_GRACE_S = float(os.environ.get("FLEET_INTAKE_AUTOSTART_GRACE_S", "240") or 240)

#: How long to wait after an attempt that never became live. Without this the router would
#: spawn a browser every drain pass -- every fifteen seconds -- for as long as the goal waits.
AUTOSTART_BACKOFF_S = float(os.environ.get("FLEET_INTAKE_AUTOSTART_BACKOFF_S", "900") or 900)

#: The free-space floor an autostarted (non-bench) run admits against. Small on purpose -- it is
#: not protecting a Docker build, it is protecting the machine's ability to keep working at all.
#: Sized from what stops being possible below it: git cannot write a loose object, the fleet
#: cannot write a transcript, and the browser cannot cache. Measured 2026-09-14, all three
#: failed together at zero, and 174 MB was not enough for a commit of five files.
#:
#: See the long note at its use site for why this is not 0 (an "ordinary" goal wrote 5.43 GB)
#: and not the bench's 6 GB (which blocked everything, silently, for twenty-five minutes twice).
AUTOSTART_DISK_FLOOR_GB = float(os.environ.get("FLEET_AUTOSTART_DISK_FLOOR_GB", "2") or 2)


def _operator_set_a_disk_floor() -> bool:
    """Has the operator chosen a disk floor in the cockpit, or is there nothing to respect?

    `settings_disk_floor` substitutes a default when the key is absent, so it cannot answer this
    on its own -- a sentinel default is passed and a negative result means "no line in
    settings.txt". Any real choice, including 0, counts: the cockpit clamps that control to
    0..100 and therefore offers 0, so treating it as "unset" would be substituting a number for
    one the operator picked.

    Never raises. An unreadable settings file reads as "nothing chosen", which lands on the
    autostart default -- the conservative side, since the alternative is inheriting the bench
    reserve and admitting nothing.
    """
    # Moved next to settings_disk_floor, the function it is about, when the bench
    # orchestrators turned out to need the same answer. The reasoning above is kept here
    # because this is where it was learned.
    try:
        from relay.fleet_runner import operator_set_a_disk_floor
        return operator_set_a_disk_floor()
    except Exception:
        return False

#: When an autostarted run gets --fanout. fleet_runner exposes the flag and threads it into
#: run_relay_fleet(fanout=...), but autostart_fleet never passed it, so a goal that arrives
#: from the tunnel could never be split -- the one path where a phone-sized instruction is
#: most likely to be "a quarter of mail", which is exactly the size problem fan-out exists
#: for. The flag's own help is the policy this honours: "Off by default -- a goal that fits
#: should not pay for a split turn and a merge turn." So it is turned on per size, not always:
#: a goal whose text is at least this many characters is long enough that a split/merge turn
#: is worth its cost. 0 (or a non-positive value) disables the size heuristic entirely.
AUTOSTART_FANOUT_MIN_CHARS = int(
    os.environ.get("FLEET_INTAKE_AUTOSTART_FANOUT_MIN_CHARS", "600") or 600)


def _truthy_env(name):
    """An explicit operator override, or None when the variable is unset.

    Returns True/False when FLEET_INTAKE_AUTOSTART_FANOUT is set to a yes/no value, and None
    when it is absent -- so the caller can tell "forced on", "forced off" and "decide by size"
    apart. A blank string is treated as unset, matching how the other flags here read .env.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _wants_fanout(goals) -> bool:
    """Should this launch be fan-out-CAPABLE? Yes, unless an operator said otherwise.

    CAPABILITY, NOT DECISION, AND IT NO LONGER JUDGES ANYTHING. This used to run
    `splittability.judge` over the goals and answer "no" when none looked splittable. That was
    the same judgement RelayWorker makes per goal, run earlier and with less information, and
    its only possible effect was to take the question away from the real judge -- for every
    goal in the run, including goals added mid-run that nobody has seen yet. Answering "no"
    here is the one answer that cannot be revisited later, which is why it now requires an
    operator to say it.

    Nothing is spent by saying yes. RelayWorker computes
    `self.fanout = bool(fanout) and _depth0 and _goal_splittable`, so a goal judged NO_SPLIT
    costs no turn at all; only a SPLIT or an UNCERTAIN triage spends one asking the agent,
    which may itself answer NO_SPLIT. See docs/fanout_contract.md for the whole staircase.

    WHERE THE OLD MEASUREMENT WENT. The corpus study that replaced the length proxy (1214 real
    goal occurrences; 60.8% called SPLIT by length alone against 0.7% by independence) is the
    reason `splittability` exists and is still what RelayWorker consults. It was never a reason
    to gate the capability -- the judge it justifies runs downstream of this function.

    The two off switches: FLEET_INTAKE_AUTOSTART_FANOUT (explicit, wins outright) and
    AUTOSTART_FANOUT_MIN_CHARS <= 0 (kept as the documented kill switch it already was).
    """
    override = _truthy_env("FLEET_INTAKE_AUTOSTART_FANOUT")
    if override is not None:
        return override
    # THE SWITCH IN FRONT OF THE OPERATOR COUNTS AS THE OPERATOR SAYING OTHERWISE. The
    # docstring above promises "yes, unless an operator said otherwise" and then offered only
    # an environment variable and a kill switch as ways to say it -- while the cockpit has a
    # fan-out control, writes `fanout=` from it, and honours it for its own launches. On the
    # autostart path the setting was never read, so settings.txt said off and every
    # autostarted run carried --fanout. A control that does nothing is worse than no control:
    # the operator believes the question is settled.
    #
    # Unset stays unset: None falls through to the size rule below, so a machine where nobody
    # has touched the switch behaves exactly as before.
    try:
        from relay.fleet_runner import settings_fanout
        chosen = settings_fanout()
    except Exception:
        chosen = None
    if chosen is not None:
        return chosen
    if AUTOSTART_FANOUT_MIN_CHARS <= 0:
        return False
    # CAPABILITY, NOT DECISION -- so it is on. This function chooses whether the RUN can fan
    # out at all; RelayWorker then judges each goal on its own (offline triage, then the agent,
    # which may answer NO_SPLIT), and a goal judged NO_SPLIT costs nothing. Answering "no" here
    # is the only answer that cannot be revisited: it takes the question away from the judge
    # for every goal in the run, including goals added mid-run that nobody has seen yet.
    return True


def launch_creationflags() -> int:
    """Windowless creation flags for an unattended fleet child.

    Keep this identical to the repository-wide policy. CREATE_NEW_PROCESS_GROUP used to be
    ORed in here, but a CREATE_NO_WINDOW child has no shared console for Ctrl+C/CTRL_BREAK and
    this repository does not use GenerateConsoleCtrlEvent as a shutdown channel.
    """
    from tools.childproc import headless_creationflags
    return headless_creationflags()


def _autostart_path(state_dir) -> str:
    return os.path.join(state_dir or FLEET_STATE_DIR, AUTOSTART_STATE)


def _read_autostart(state_dir) -> dict:
    try:
        with open(_autostart_path(state_dir), encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _write_autostart(state_dir, rec: dict) -> None:
    path = _autostart_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


#: How long an `owner_pid` is believed. A pid is not a durable identity -- Windows reuses
#: them -- so after this the entry is treated as abandoned whatever the pid says. An hour is
#: far longer than any fleet startup and far shorter than a pid recycles in practice.
OWNER_PID_GRACE_S = 3600.0


def _still_owned(path: str, alive_cache=None) -> bool:
    """Is this pending entry still held by a live process that wrote it about itself?

    `relay/fleet_runner.py` writes one entry per CLI goal the moment it is known, so a
    submission is visible before anything can refuse it -- but tasks/pending/ is not a display
    surface, it is this module's inbox, and dispatch_once claims everything in it. Without
    this the router could deliver a goal into a live fleet while the process that wrote it was
    starting up to run the same goal itself. Measured artifact: a cli*.delivered.json beside
    the cli*.json the same run wrote.

    ABANDONED IS THE CASE THAT MUST STILL WORK. A run that dies during startup leaves its
    entry behind with a dead pid, and taking it then is the whole point of recording it -- an
    unclaimed entry is the difference between "refused" and "never happened". So this answers
    "still mine", not "mine at all".

    UNREADABLE MEANS NOT OWNED. A file that vanished (another router claimed it) or will not
    parse must fall through to the claim, where the rename decides and a parse error becomes
    a done-record. Refusing to claim on a read failure would strand it silently instead.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            job = json.load(fh) or {}
    except Exception:
        return False
    pid = job.get("owner_pid")
    if not pid:
        return False
    try:
        age = time.time() - float(job.get("created") or 0.0)
    except Exception:
        age = 0.0
    if age > OWNER_PID_GRACE_S:
        return False
    # ONE LIVENESS QUERY PER PID PER PASS. `_pid_alive` shells out to tasklist, measured at
    # 316 ms here, and a CLI run records ONE ENTRY PER GOAL -- all of them owned by the same
    # process. Asking once per entry made a ten-goal run cost 3.2 s of every pass, against a
    # --poll-s default of 2.0: the router would have spent its life in tasklist. The cache is
    # a dict handed in by the caller and thrown away with the pass, so it cannot go stale and
    # no test has to know it exists.
    if alive_cache is None:
        return _pid_alive(pid)
    key = str(pid)
    if key not in alive_cache:
        alive_cache[key] = _pid_alive(pid)
    return alive_cache[key]


def _pid_alive(pid) -> bool:
    """Whether a launched runner is still around. Windows has no os.kill(0), so ask the OS."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20,
                             creationflags=childproc.headless_creationflags())
        return str(pid) in (out.stdout or "")
    except Exception:
        # UNKNOWN IS TREATED AS ALIVE, deliberately. The only thing this answer gates is
        # whether to launch ANOTHER fleet, and two fleets sharing one Edge is the failure this
        # whole check exists to prevent. Waiting out the grace period costs a delay.
        return True


def autostart_status(state_dir=None, now=None) -> tuple:
    """(may_start, reason). Reads only; the reason is what the record will say.

    Three states have to be told apart, and conflating any two of them is how this goes wrong:
    a run that is already coming up (wait), an attempt that died (back off, then retry), and
    nothing in flight (start). The first two both look like "no fleet is live".
    """
    now = time.time() if now is None else now
    if not AUTOSTART:
        return False, "autostart is off (FLEET_INTAKE_AUTOSTART)"
    if fleet_is_live(state_dir):
        return False, "a fleet is already running"
    rec = _read_autostart(state_dir)
    started = float(rec.get("started_at") or 0)
    if started:
        age = now - started
        outcome = rec.get("outcome")
        if outcome == "launched":
            if age < AUTOSTART_GRACE_S and _pid_alive(rec.get("pid")):
                return False, ("a fleet launched %.0fs ago is still coming up (pid %s)"
                               % (age, rec.get("pid")))
            # A LAUNCH WHOSE PROCESS IS GONE WHILE NOTHING IS LIVE HAS FAILED, and it is not
            # recover_failed_autostart's job to say so first. That runs on the next drain pass,
            # and between the crash and that pass this function would otherwise answer "nothing
            # in flight" -- which is a relaunch every fifteen seconds against whatever made the
            # first one die. Backing off from the launch time covers the gap.
            if age < AUTOSTART_BACKOFF_S:
                return False, ("the fleet launched %.0fs ago is not running and its process is "
                               "gone; waiting out the %.0fs backoff"
                               % (age, AUTOSTART_BACKOFF_S))
        elif outcome in ("never_became_live", "launch_failed") and age < AUTOSTART_BACKOFF_S:
            return False, ("the last autostart %s %.0fs ago; waiting out the %.0fs backoff"
                           % (outcome.replace("_", " "), age, AUTOSTART_BACKOFF_S))
    return True, "no run in flight and no recent attempt"


def _agent_url() -> str:
    return (os.environ.get("MCP_FLEET_AGENT_URL")
            or os.environ.get("MCP_IMPL_AGENT_URL") or "").strip()


#: How long to wait before believing a fleet started. Covers the import-and-argparse class
#: only -- see the comment at the launch site.
STARTUP_LIVENESS_S = 2.0


def _coordinator_log_tail(state_dir, lines=3):
    """The last few lines of the newest coordinator log, for a launch that did not survive.

    The traceback is already on disk; nothing was reading it. Never raises -- a failure to
    explain a failure must not become a second failure.
    """
    try:
        import glob as _glob
        logs = _glob.glob(os.path.join(state_dir, "coordinator_*.log"))
        if not logs:
            return ""
        newest = max(logs, key=lambda p: os.stat(p).st_mtime)
        with open(newest, encoding="utf-8", errors="replace") as fh:
            rows = [r.strip() for r in fh.read().splitlines() if r.strip()]
        return " | ".join(rows[-lines:])[:400]
    except Exception:
        return ""


def autostart_fleet(goals, state_dir=None, now=None, launcher=None) -> dict:
    """Launch a fleet for `goals` (a list of goal dicts). Returns what happened.

    THE GOALS GO IN THE LAUNCH, NOT THROUGH add_goal. A fleet started with no goals exits at
    once, and delivering them afterwards means waiting for it to come up -- which the router
    must not do inside a drain pass. So the caller hands them over here and they become the
    run's goals-file: one JSON object per line, the format bench/review_build_goals.py writes
    and _read_goals expects.

    `launcher` is injectable so a test can drive this without starting a browser. Nothing in
    production passes it.
    """
    now = time.time() if now is None else now
    sd = state_dir or FLEET_STATE_DIR
    # NOTHING IN A TEST RUN OPENS A BROWSER. This is the same systemic guard
    # tools/notify_ops.notify_desktop uses, and for the same reason: this is the single point
    # that spawns a real fleet, so refusing here once is worth more than every test remembering
    # to patch. It became necessary the moment FLEET_INTAKE_AUTOSTART went into .env -- until
    # then AUTOSTART was False everywhere and no test could reach this line by accident.
    # PYTEST_CURRENT_TEST is not a complete signal (it is unset during collection and does not
    # always cross into subprocesses), which is why conftest also forces AUTOSTART off; this is
    # the layer that catches a test the fixture missed.
    if os.environ.get("PYTEST_CURRENT_TEST") and launcher is None:
        return {"ok": False, "detail": "refused: a real fleet launch under pytest"}
    goals = [g for g in (goals or []) if (g or {}).get("text")]
    if not goals:
        return {"ok": False, "detail": "no goals to start a fleet for"}
    url = _agent_url()
    if not url:
        return {"ok": False, "detail": "no agent URL (MCP_FLEET_AGENT_URL / MCP_IMPL_AGENT_URL)"}

    os.makedirs(sd, exist_ok=True)
    goals_file = os.path.join(sd, "autostart.goals.jsonl")
    with open(goals_file, "w", encoding="utf-8", newline="\n") as fh:
        for g in goals:
            fh.write(json.dumps(g, ensure_ascii=False) + "\n")

    # THE DISK GATE IS FOR BENCH EVALS, AND A GOAL FROM A PHONE IS NOT ONE.
    #
    # The floor exists because five concurrent SWE-bench Docker builds once filled C: and
    # corrupted WSL, so an eval-bearing tab is admitted only if free space survives the build it
    # is about to start. relay_fleet.disk_admission_ok and --disk-floor-gb both say the same
    # thing about the other case: "normal (non-bench) use may not want a reserve", "0 = disable
    # the disk gate (normal, non-bench use)". Autostart never passed the flag, so a tunnel goal
    # inherited the bench floor -- 6 GB by default, 3 GB as configured here.
    #
    # What that cost: with C: below the floor the run admits NOTHING. Every sweep refuses, breaks
    # and defers, forever -- there is no timeout and no give-up. The reason is printed once a
    # minute into the coordinator's log, which is the one place a person holding a phone cannot
    # look. The submitter has already been told "queued -- it will be picked up by its runner".
    # So the goal is accepted, a browser is opened (spending more of the disk that was the
    # problem), and nothing ever runs, silently. The same shape was measured before from the
    # desk: two calibration runs sat admitting nothing for twenty-five minutes each and a stack
    # dump was the only way to find out.
    #
    # An ordinary goal writes a transcript and some status JSON -- kilobytes. Gating it behind a
    # multi-gigabyte Docker reserve was a category error, and the failure it produced (silent,
    # unobservable from the device that asked) is worse than the disk pressure it was avoiding.
    # Bench runs still pass their own floor and keep the protection that was actually earned.
    #
    # BUT NOT ZERO, AND "KILOBYTES" WAS WRONG. Measured 2026-09-14: an ordinary goal told to back
    # a folder up before editing it wrote a **5.43 GB** archive (zip_create walked its own output
    # -- fixed in tools/archive_ops, but the premise it broke is the one this line rested on).
    # With the gate disabled the fleet kept admitting while C: drained to **zero bytes**, which
    # took out git, the fleet's own writes and a business folder's backups together. A goal that
    # writes kilobytes is the common case, not a guarantee, and this is the last brake before the
    # machine stops working at all.
    #
    # The floor that silently blocked everything is not being restored either: it was the SIZE
    # (a Docker-sized reserve for a goal that needs none) and the SILENCE (a log line nobody
    # could see) together that made it unusable. `_note_disk_defer` now reports a block outward
    # once it outlasts DISK_DEFER_ALERT_AFTER_S, so a floor this small can only ever cost a
    # notification -- never another twenty-five silent minutes.
    #
    # AND IT IS A DEFAULT, NOT AN OVERRIDE. fleet_runner resolves the floor as
    # "CLI --disk-floor-gb >= 0 -> settings.txt disk_floor_gb -> env", so passing the flag at all
    # BEATS the cockpit's own control. Measured 2026-09-15: the settings panel showed 1 GB, the
    # operator had set it there, and an autostarted run used 2 -- the panel was displaying a
    # number the run did not use. (It was worse before, at 0: the same override, further from
    # the setting.) The cockpit clamps that control to 0..100, so 1 is a choice it offers and
    # 0 is too; silently substituting a number for one the operator chose makes the panel lie,
    # which is the defect class this session has spent its length removing.
    #
    # So the flag goes on ONLY when settings.txt carries no floor -- which is the case the
    # hard-coding was written for, a tunnel goal inheriting the bench reserve because nobody
    # had chosen anything. When the operator has chosen, their choice is left to the chain, and
    # the cockpit's live `set_disk_floor_gb` keeps working because nothing is pinned at launch.
    cmd = [sys.executable, "-m", "relay.fleet_runner",
           "--goals-file", goals_file, "--agent-url", url, "--state-dir", sd]
    if not _operator_set_a_disk_floor():
        cmd += ["--disk-floor-gb", str(AUTOSTART_DISK_FLOOR_GB)]
    # FAN-OUT WAS WIRED EVERYWHERE BUT HERE. fleet_runner parses --fanout and passes it to
    # run_relay_fleet(fanout=...); the worker gates the split turn itself (depth 0 only). The
    # missing link was this command line: without the flag an autostarted goal could never
    # split, however large. Added by goal size so a goal that fits pays nothing for it.
    if _wants_fanout(goals):
        cmd.append("--fanout")
    try:
        if launcher is not None:
            pid = launcher(cmd)
        else:
            # CREATE_NO_WINDOW, NOT DETACHED_PROCESS -- AND THE DIFFERENCE IS A WINDOW ON THE
            # OPERATOR'S DESKTOP.
            #
            # DETACHED_PROCESS gives the child NO console. That is fine for the child, but the
            # child here is .venv\Scripts\python.exe, a shim that execs the real interpreter, and
            # a console application started by a process with no console gets a BRAND NEW one --
            # which Windows Terminal then puts on screen. Measured: the visible window belonged
            # to PID 18152 (Python310\python.exe -m relay.fleet_runner), class
            # CASCADIA_HOSTING_WINDOW_CLASS, hosting a PseudoConsole; its parent was the shim
            # this call spawned. So the flag was applied to the process that did not need it and
            # missed the one that did, and every goal sent from a phone popped a black window on
            # a desktop nobody was sitting at.
            #
            # CREATE_NO_WINDOW gives this process a console with no window, and descendants
            # INHERIT it rather than allocating their own -- which is what reaches the grandchild.
            # No extra process-group flag: shutdown is file/taskkill based, and a windowless child
            # cannot receive console control events from a console it does not share.
            kwargs = {"cwd": REPO}
            if os.name == "nt":
                kwargs["creationflags"] = launch_creationflags()
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
            pid = proc.pid
            # A PID IS NOT A RUN. The Popen used to be discarded here, so nothing could ask
            # whether the child survived -- and on 2026-09-17 one died a second after launch
            # while the queue recorded it as started and the task moved to done/. Two seconds
            # covers the import-and-argparse class: a missing module, a syntax error, an
            # UnboundLocalError in main(). It does NOT cover a run that comes up and fails
            # later; the ack receipt and the reconcile pass own that, and this does not
            # replace them.
            try:
                rc = proc.wait(timeout=STARTUP_LIVENESS_S)
            except subprocess.TimeoutExpired:
                rc = None                      # still running, which is what we wanted
            if rc is not None:
                detail = "the fleet exited %s within %gs of launch" % (rc,
                                                                       STARTUP_LIVENESS_S)
                tail = _coordinator_log_tail(sd)
                if tail:
                    detail += " -- " + tail
                rec = {"started_at": now, "pid": pid, "outcome": "died_at_startup",
                       "error": detail, "goals": goals, "goals_file": goals_file}
                _write_autostart(sd, rec)
                return {"ok": False, "detail": detail}
    except Exception as exc:
        rec = {"started_at": now, "pid": None, "outcome": "launch_failed",
               "error": "%s: %s" % (type(exc).__name__, exc),
               "goals": goals, "goals_file": goals_file}
        _write_autostart(sd, rec)
        return {"ok": False, "detail": rec["error"]}

    # THE GOALS ARE IN THE RECORD, NOT ONLY IN THE LAUNCH. If this run never comes up they have
    # to be recoverable: a goal that vanished because a browser failed to open is exactly the
    # failure that reads as "it was never sent".
    _write_autostart(sd, {"started_at": now, "pid": pid, "outcome": "launched",
                          "goals": goals, "goals_file": goals_file})
    cockpit = ensure_cockpit()
    return {"ok": True, "pid": pid, "goals": len(goals), "goals_file": goals_file,
            "cockpit": cockpit}


#: The built cockpit. A module constant rather than a path computed inside the function, so a
#: test can point it at a file that is not there -- "never built" is a normal state on a host
#: that only serves, and it must not turn a working fleet launch into a failure.
COCKPIT_EXE = os.path.join(REPO, "ui", "FleetCockpit.exe")

#: argv words that mean this FleetCockpit process is one of the OTHER windows the same
#: executable serves. Matching the image name alone would read the approval prompt or the
#: authority dashboard as "the cockpit is up" and leave the fleet unwatched -- the mistake
#: tools/notify_ops.cockpit_running documents from the opposite direction.
_COCKPIT_OTHER_WINDOWS = ("--approval-gate", "--authority")


def cockpit_is_up() -> bool:
    """True iff the ORDINARY cockpit window is already running. Never raises.

    A process whose cmdline cannot be read is counted as the cockpit: one unwatched fleet is a
    smaller harm than a second window, because the ordinary cockpit takes NO mutex (unlike the
    other two windows) and a duplicate really does appear.
    """
    try:
        import psutil
    except Exception:
        return True          # cannot tell -> do not launch
    want = os.path.basename(COCKPIT_EXE).lower()
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            if (proc.info.get("name") or "").lower() != want:
                continue
            argv = proc.info.get("cmdline") or []
            if any(str(a).lower() in _COCKPIT_OTHER_WINDOWS for a in argv[1:]):
                continue     # a different window of the same exe
            return True
    except Exception:
        return True
    return False


def ensure_cockpit() -> str:
    """Put the cockpit on screen for a fleet that autostart brought up. Returns what happened.

    A FLEET STARTED FROM A PHONE HAS NOBODY WATCHING IT. start_all.ps1 opens the cockpit
    alongside everything else, so a fleet the operator starts at the desk is visible. Autostart
    is the other door: a goal arrives over the tunnel, a run begins, and the only thing on the
    desktop is the runner's console. Measured on 2026-09-07 -- a natural-language goal reached
    the fleet and completed in 2m10s with FleetCockpit.exe built but not running, so the fleet
    ran entirely unobserved and the console was the only surface.

    Bounded, because the ordinary cockpit takes NO mutex: unlike --approval-gate and --authority
    a second launch really does open a second window, and an unbounded launcher once opened
    forty-two of them and took the machine down (see tools/notify_ops). So this checks first and
    launches at most one.

    Best effort throughout. A missing build, no psutil, a refused spawn: none of them may turn a
    fleet that IS running into a reported failure, so every path returns a string and none raise.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "skipped (test run)"
    if os.name != "nt":
        return "skipped (not windows)"
    if not os.path.isfile(COCKPIT_EXE):
        return "unavailable (FleetCockpit.exe not built)"
    if cockpit_is_up():
        return "already running"
    try:
        subprocess.Popen(
            [COCKPIT_EXE], cwd=REPO,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "launched"
    except Exception as exc:
        return "launch failed: %s: %s" % (type(exc).__name__, exc)


#: How far BEFORE a launch a run's own start stamp may sit and still be counted as that
#: launch's run. The launch records its time before Popen and the run writes status.json a few
#: seconds later, so in practice the stamp is always later; the slack only absorbs a clock that
#: ticks backwards slightly, and stays far too small to match the previous run.
_AUTOSTART_START_SLACK_S = 5.0


def _run_started_since(state_dir, since) -> bool:
    """Did a fleet run START at or after `since`? Never raises.

    Distinct from fleet_is_live, which asks whether one is running NOW. This reads the same
    status.json but looks at the run's own `started` stamp, so a run that has already finished
    still answers yes -- which is the whole point, since a finished run and a run that never
    came up look identical from the outside.

    A stale status.json from an EARLIER run has a `started` older than `since` and correctly
    answers no, so this cannot mistake the previous run for this launch's.
    """
    try:
        sp = os.path.join(state_dir, "status.json")
        if not os.path.isfile(sp):
            return False
        with open(sp, encoding="utf-8-sig") as fh:
            started = float((json.load(fh) or {}).get("started") or 0)
        return started > 0 and started >= (float(since) - _AUTOSTART_START_SLACK_S)
    except Exception:
        return False


def recover_failed_autostart(state_dir=None, now=None) -> list:
    """Put back the goals of an autostart that never became live. Returns what was restored.

    Without this the goals are gone: they left for_fleet/ when the launch was made, and the run
    that was supposed to carry them died. Restoring them into for_fleet/ is what makes the next
    live fleet -- started by hand or by the next autostart -- pick them up, and it is why
    autostart_fleet writes them into its record rather than only into the goals file.
    """
    now = time.time() if now is None else now
    sd = state_dir or FLEET_STATE_DIR
    rec = _read_autostart(sd)
    if not rec or rec.get("outcome") not in ("launched",):
        return []
    started = float(rec.get("started_at") or 0)
    if now - started < AUTOSTART_GRACE_S:
        return []
    if fleet_is_live(sd):
        _write_autostart(sd, dict(rec, outcome="became_live"))
        return []
    # DID IT EVER RUN -- not "is it running now". fleet_is_live answers the second question,
    # and for a run that FINISHED inside the grace window the two answers differ: the fleet is
    # gone and the pid has exited for exactly the same reason a successful run leaves. Asking
    # only the present-tense question filed a completed run as one that never came up and
    # handed its goal back to the queue, so the work would be done twice.
    #
    # Measured on the first real autostart: launched 15:14:19, finished DONE at 15:17:18,
    # grace expired 15:18:19, and at 15:18:31 this function restored the goal it had already
    # completed. Any run shorter than AUTOSTART_GRACE_S hits it, which is most small goals.
    if _run_started_since(sd, started):
        _write_autostart(sd, dict(rec, outcome="became_live"))
        return []
    if _pid_alive(rec.get("pid")):
        return []
    restored = []
    ensure_dirs()
    for g in rec.get("goals") or []:
        text = (g or {}).get("text")
        if not text:
            continue
        jid = uuid.uuid4().hex[:12]
        if _write_for_fleet(jid, text):
            restored.append(jid)
    _write_autostart(sd, dict(rec, outcome="never_became_live", restored=restored))
    return restored


def read_for_fleet(text: str) -> tuple:
    """A parked file's contents as (goal, priority). The inverse of `_write_for_fleet`.

    OLD FILES ARE PLAIN TEXT AND MUST KEEP WORKING. Anything already sitting in for_fleet/
    when this shipped is a bare goal, and so is every goal parked without a field worth
    carrying -- which is nearly all of them. Only a goal that HAS something to carry is
    written as JSON, so the format stays the cheap one by default and the reader tells them
    apart by looking.

    A goal whose own text begins with a brace is not mistaken for one: the parse has to
    succeed AND produce a mapping with a `text` in it, and anything else falls back to
    treating the whole contents as the goal, which is what it is.
    """
    body = text or ""
    if body.lstrip().startswith("{"):
        try:
            obj = json.loads(body)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("text"), str):
            return obj["text"], bool(obj.get("priority"))
    return body, False


def _write_for_fleet(jid, goal, priority: bool = False) -> bool:
    """Put a waiting goal on disk so a reader never sees half of one. Returns whether it landed.

    THE EMPTY CHECK WAS NOT A TORN-WRITE CHECK. `_deliver_waiting_goals` skips a file whose
    contents are blank, which catches a write that had not started -- and nothing caught one
    that had started and not finished. All four writers used a plain `open(..., "w")`, so a
    reader arriving mid-write got a PREFIX of the goal, which is not blank, and handed the
    fleet an instruction that stops in the middle of a sentence. A truncated goal is worse
    than a missing one: it looks like something the operator wrote.

    Same shape as `write_command`: a `.tmp` beside it, then `os.replace`, which is atomic on
    NTFS. The reader already ignores anything that is not `.txt`, so a writer mid-flight is
    invisible for free. The rename is retried briefly because Windows refuses it while another
    process holds the target open -- measured there, and the same reason it is retried there.
    """
    ensure_dirs()
    path = _p("for_fleet", "%s.txt" % jid)
    tmp = _p("for_fleet", "%s.tmp" % jid)
    # JSON ONLY WHEN THERE IS SOMETHING TO CARRY. A parked goal used to be its text and
    # nothing else, so any field the door accepts was lost the moment no fleet was running --
    # the same goal delivered a second later kept it. `read_for_fleet` reads both shapes.
    body = json.dumps({"text": goal, "priority": True}, ensure_ascii=False) if priority \
        else goal
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    except OSError:
        return False
    until = time.time() + 2.0
    while True:
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if time.time() > until:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def _park_in_for_fleet(goal, jid, priority=False):
    """Write the goal into for_fleet/<jid>.txt so a waiting goal is visible on disk, not just
    named in a status string. Returns the handoff path label either way. Best-effort: a goal
    the queue cannot see is the very failure this module exists to prevent, but if the write
    itself fails the caller still reports "waiting" rather than a false "delivered"."""
    _write_for_fleet(jid, goal, priority=priority)
    return "for_fleet/%s.txt" % jid


def fleet_handoff(goal: str, jid: str, state_dir=None, priority: bool = False):
    """Deliver a fleet-bound goal. Returns the (status, result) the job record should carry.

    `priority` HAS TO SURVIVE ALL THREE ROUTES OR IT IS A LIE ON ONE OF THEM. A goal leaves
    here by joining a live run, by starting one, or by waiting in for_fleet/ -- and the third
    stored nothing but the goal's text, so a field added at the door and not here would be
    silently dropped by whichever route the machine happened to take. That is the shape
    resume_conv had: set at one end, read at the other, lost in between, and working on one
    path while guessing on another.
    """
    if not (goal or "").strip():
        return "error", {"handoff": "for_fleet/%s.txt" % jid, "detail": "empty goal"}
    if fleet_is_live(state_dir):
        # Clear any stale receipt for this id before queueing, so a confirmation seen later
        # belongs to THIS handoff and not a previous run's leftover file.
        try:
            os.remove(_ack_path(jid, state_dir))
        except OSError:
            pass
        add_goal_to_live_fleet(goal, state_dir, priority=priority, jid=jid)
        # RECORD THE OPEN CLAIM so a later pass can check it. "dispatched" is written to done/
        # now, but done/ is terminal -- nothing re-reads it -- so on its own it can never be
        # corrected when the fleet turns out to have died inside the stale-status window. This
        # marker is the one thing _reconcile_landings scans: it holds the goal so a goal lost to
        # that window can be re-queued, and it is deleted the moment the ack proves the goal
        # landed. Best-effort: a goal already on the channel must not be lost because the marker
        # write failed, so the delivery above happens first and this cannot raise past here.
        try:
            with open(_p("awaiting_ack", "%s.json" % jid), "w", encoding="utf-8") as _mf:
                json.dump({"id": jid, "goal": goal, "ts": time.time(),
                           # CARRIED SO THE RE-QUEUE IS NOT A DEMOTION. _reconcile_landings
                           # re-parks from this marker when the ack never arrives, and a goal
                           # that came back from a lost delivery is not less urgent than it
                           # was when it left.
                           "priority": bool(priority),
                           "ack": _ack_path(jid, state_dir)}, _mf, ensure_ascii=False)
        except OSError:
            pass
        # STILL "dispatched", because the goal is on the channel and joining the run in flight
        # is the right destination. But delivery is now CHECKABLE: the command carries an ack
        # path, the fleet drops a receipt when it reads it, and fleet_landing_confirmed(jid)
        # (or the reconcile pass) tells a delivered goal apart from one lost to the stale
        # window fleet_is_live cannot close on its own. `ack` names where that receipt lands.
        return "dispatched", {"handoff": "for_fleet/%s.txt" % jid,
                              "delivered": "add_goal", "note": "queued into the running fleet",
                              "ack": _ack_path(jid, state_dir)}
    if AUTOSTART:
        may, why = autostart_status(state_dir)
        if may:
            # jid carried into the goal dict, same as add_goal_to_live_fleet's live path
            # above -- _read_goals_file() keeps a JSON-object goal line verbatim, so this
            # survives into the cold-started fleet's Worker unchanged. Without it, a goal
            # delivered via autostart could never be joined back to its admission record.
            out = autostart_fleet([{"text": goal, "jid": jid,
                                    "priority": bool(priority)}], state_dir)
            if out.get("ok"):
                return "dispatched", {"handoff": "for_fleet/%s.txt" % jid,
                                      "delivered": "autostart",
                                      "note": "started a fleet for this goal (pid %s)"
                                              % out.get("pid")}
            return "awaiting_fleet", {"handoff": _park_in_for_fleet(goal, jid, priority),
                                      "note": "autostart could not start a fleet: %s"
                                              % out.get("detail")}
        return "awaiting_fleet", {"handoff": _park_in_for_fleet(goal, jid, priority), "note": why}
    # SAYS IT IS WAITING, rather than "dispatched". The old wording claimed delivery for a
    # file nobody read, and a status that overstates what happened is how a queue goes
    # unnoticed for months.
    return "awaiting_fleet", {"handoff": _park_in_for_fleet(goal, jid, priority),
                              "note": "no fleet run is in flight; the goal waits for one"}


#: How long a "dispatched" goal may wait for its landing ack before _reconcile_landings treats
#: it as lost to the stale-status window and re-queues it. Must be comfortably longer than
#: FLEET_LIVE_MAX_AGE_S (the width of that window) plus one drain interval, so a fleet that is
#: genuinely alive but slow to read its command channel is not re-queued out from under itself.
RECONCILE_ACK_GRACE_S = float(os.environ.get("FLEET_RECONCILE_ACK_GRACE_S", "90") or 90)


def _reconcile_landings(now_ts=None, state_dir=None):
    """Turn every "dispatched" claim into a checked outcome, using the fleet's landing acks.

    THE GAP THIS CLOSES. fleet_handoff files a goal "dispatched" the instant fleet_is_live() is
    True, but that check trusts a status.json up to FLEET_LIVE_MAX_AGE_S old: a run that had
    already died still read as live for up to that long, and a goal queued in that window went
    into commands.d/ that no live reader would ever consume. "dispatched" was the only record,
    done/ is terminal, and nothing ever revisited the claim -- so a goal lost this way looked
    exactly like one delivered. The receiving side now drops an ack when it actually reads a
    command; this pass is the reader of those acks.

    For each open claim in awaiting_ack/:
      * ack present            -> the fleet really took it. Record done/<id>.landed.json and
                                  drop the marker. This is the confirmation "dispatched" could
                                  never give on its own.
      * no ack, past the grace -> lost to the stale window. Re-queue it as a for_fleet/ waiter
                                  (the same channel _deliver_waiting_goals drains on the next
                                  live pass, which will hand it over WITH a fresh ack), record
                                  done/<id>.reconcile-requeued.json, and drop the marker.
      * no ack, still in grace -> leave it; a live-but-slow fleet has not answered yet.

    Runs on every drain pass. Costs one listdir when there are no open claims.
    """
    out = []
    ensure_dirs()
    # AGE IS MEASURED ON THE WALL CLOCK, not on now_ts. now_ts is a record stamp the caller may
    # pass as any monotonic value (tests pass small integers), and comparing a marker ts taken
    # from time.time() against that would make the grace meaningless. now_ts is only written
    # into the done/ records below.
    now = time.time()
    try:
        names = sorted(os.listdir(_p("awaiting_ack", "")))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        mpath = _p("awaiting_ack", name)
        try:
            with open(mpath, encoding="utf-8") as fh:
                marker = json.load(fh)
        except Exception:
            continue  # unreadable -- leave for manual inspection rather than losing the claim
        jid = marker.get("id", name[:-5])
        goal = marker.get("goal", "")
        if fleet_landing_confirmed(jid, state_dir):
            receipt = read_ack_receipt(jid, state_dir) or {}
            if receipt.get("rejected"):
                # THE FLEET READ THIS COMMAND AND REFUSED IT (e822fb6's admit_command /
                # validate_command). "Landed" only ever meant "the fleet took it off the
                # channel" -- it never meant "the fleet queued it as a goal". Recording this
                # as "dispatched" told the operator a goal had joined the run when it never
                # did, and nothing else was ever going to say otherwise: a refused command
                # produces no worker, so no worker_done row will ever arrive to correct it,
                # and job_status() would have sat on "dispatched"/"unknown" until
                # JOB_STATUS_UNKNOWN_AFTER_S pretending the wait might still resolve. Written
                # straight to the OUTCOME path (not a side "*.landed.json" file) so
                # job_status()'s very first check -- os.path.isfile(outcome_path) -- reports
                # "refused" immediately instead of decaying into "nothing further is known".
                errors = list(receipt.get("errors") or [])
                rec = {"id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": now_ts,
                       "status": "refused",
                       "detail": "the fleet command channel rejected this goal at admission",
                       "result": {"landing_confirmed": True, "rejected": True,
                                  "errors": errors, "ack": _ack_path(jid, state_dir)},
                       "error": ("command rejected: " + "; ".join(errors))[:500] if errors
                                else "command rejected"}
                try:
                    with open(_p("done", "%s.outcome.json" % jid), "w",
                              encoding="utf-8") as fh:
                        json.dump(rec, fh, ensure_ascii=False, indent=2)
                except OSError:
                    pass
                try:
                    os.remove(mpath)
                except OSError:
                    pass
                out.append(rec)
                continue
            rec = {"id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": now_ts,
                   "status": "dispatched", "result": {"landing_confirmed": True,
                   "ack": _ack_path(jid, state_dir)}, "error": None}
            try:
                with open(_p("done", "%s.landed.json" % jid), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
            try:
                os.remove(mpath)
            except OSError:
                pass
            out.append(rec)
            continue
        age = now - float(marker.get("ts", now))
        if age <= RECONCILE_ACK_GRACE_S:
            continue  # live-but-slow fleet may still read it; do not re-queue yet
        # Past the grace with no ack: the goal was lost into the stale-status window. Put it
        # back on the waiter channel so a live fleet picks it up again, this time with an ack
        # we can confirm. Written before the marker is removed, so a crash between the two
        # leaves a waiter (re-tried) rather than nothing (lost).
        requeued = False
        if (goal or "").strip():
            try:
                requeued = _write_for_fleet(jid, goal,
                                            priority=bool(marker.get("priority")))
            except OSError:
                pass
        rec = {"id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": now_ts,
               "status": "awaiting_fleet",
               "result": {"landing_confirmed": False, "reconciled": True,
                          "requeued": requeued,
                          "note": "no landing ack within %ss; goal was lost to the "
                                  "stale-status window and has been re-queued"
                                  % int(RECONCILE_ACK_GRACE_S)},
               "error": None}
        try:
            with open(_p("done", "%s.reconcile-requeued.json" % jid), "w",
                      encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        try:
            os.remove(mpath)
        except OSError:
            pass
        out.append(rec)
    return out


# ── Outcome reconciliation: done/ RECORDS DISPATCH, NOT COMPLETION ────────────────────────────
#
# THE GAP _reconcile_landings DOES NOT CLOSE. That pass confirms the fleet actually READ a
# goal off its command channel -- it turns "dispatched" into "dispatched, landing confirmed".
# It says nothing about what happened next. A goal that landed cleanly and then ran for
# twenty minutes, or a goal that landed and was still running when someone stopped the fleet,
# both still read exactly "dispatched" in done/<jid>.json today, forever, because nothing
# after landing ever looks at the job again.
#
# THE RECORD THAT ALREADY HAS THE ANSWER. relay/relay_fleet.py's RelayWorker.close() writes
# one line to <state_dir>/socket_route.jsonl for every worker that ever finishes, cockpit
# open or not, run still live or not: outcome, turns, reason, the works. It is the one
# completion record in this whole stack that does not depend on a second program (the C#
# cockpit) choosing to archive it -- .fleet/history.json is written by the cockpit FROM the
# live status.json snapshot, so a fleet started by a phone with nobody watching (see
# ensure_cockpit's own docstring) never gets archived there at all. socket_route.jsonl is
# written by the same Python process that ran the goal, unconditionally.
#
# THE JOIN KEY THIS FILE MINTS AND socket_route.jsonl DID NOT CARRY. `jid` -- the admission
# id this module hands every fleet-bound goal in add_goal_to_live_fleet / autostart_fleet --
# already reached history.json and the final sweep snapshot (see the comment on
# add_goal_to_live_fleet above), but the one line RelayWorker.close() writes on ITS way out
# never carried it. Fixed at the write site: relay/relay_fleet.py's `_socket_route().record(
# "worker_done", ...)` call now passes `jid=(self.jid or "")`. A row written before that fix
# has no jid and cannot be joined here -- there is no way to recover an id that was never
# recorded, and this module does not fall back to matching on goal text to paper over that:
# the text is truncated to 600 chars and the same goal is routinely dispatched more than once
# as a retry, so a text match risks attributing one goal's outcome to a different admission.
def _find_worker_outcome_by_jid(jid, state_dir=None):
    """The worker_done row for `jid`, or None if the ledger has never recorded one -- either
    because the worker has not finished yet, or because the row predates the jid fix above.

    Full linear scan on purpose: this is the on-demand path job_status() uses for a one-off
    "did X finish" question, and a single pass over even a many-thousand-line ledger is
    milliseconds. _reconcile_outcomes() below is the path that runs forever (once per dispatch
    tick) and is the one that actually needs a cursor to stay cheap.
    """
    if not jid:
        return None
    path = _socket_route_path(state_dir)
    found = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue          # several processes append; a torn tail is normal
                if not isinstance(row, dict) or row.get("event") != "worker_done":
                    continue
                if row.get("jid") == jid:
                    found = row       # newest wins; jid is minted once per admission so this
                                      # should never see two rows, but "last word" is the
                                      # same rule every other reader in this file uses
    except OSError:
        return None
    return found


def _socket_route_path(state_dir=None):
    """Where the fleet's own append-only completion ledger lives. Resolved through state_dir/
    FLEET_STATE_DIR exactly like status.json and the acks directory above -- in production all
    three live under the same .fleet/, and a test that redirects one must be free to redirect
    the rest identically rather than this module hard-coding relay.socket_route's own default."""
    return os.path.join(state_dir or FLEET_STATE_DIR, "socket_route.jsonl")


#: Terminal `status` values a worker_done row can carry (relay/relay_fleet.py sets self.status
#: to one of these before recording -- measured against the live ledger: done/cancelled/error/
#: stuck cover 8,014 of 8,027 rows sampled). Mapped to the status this module reports so a
#: reader never has to know the fleet's own vocabulary. "pending" (13 rows measured, all with
#: outcome=None) is not a real terminal state -- an artifact of a row recorded before the
#: worker had settled -- so it deliberately maps to nothing and is treated the same as no row
#: at all: not yet resolved.
_WORKER_STATUS_TO_JOB_STATUS = {"done": "done", "cancelled": "cancelled",
                                "error": "error", "stuck": "stuck"}

#: One cursor per state_dir. The ledger this repo already has runs to several thousand lines
#: and _reconcile_outcomes runs on every dispatch tick (every couple of seconds, forever), so
#: unlike _find_worker_outcome_by_jid's one-off scan this path re-reads only what is new.
#: A byte offset, read and written on a RAW binary handle -- Python's text-mode seek/tell only
#: promises correct behaviour for a cookie obtained from tell() on that same handle, not for an
#: offset computed independently by summing encoded line lengths, which is exactly what this
#: does. Binary mode sidesteps that entirely.
OUTCOME_CURSOR_FILE = "outcome_cursor.json"


def _outcome_cursor_path(state_dir=None):
    return os.path.join(state_dir or FLEET_STATE_DIR, OUTCOME_CURSOR_FILE)


def _read_outcome_cursor(state_dir=None):
    try:
        with open(_outcome_cursor_path(state_dir), encoding="utf-8") as fh:
            return int((json.load(fh) or {}).get("offset") or 0)
    except Exception:
        return 0


def _write_outcome_cursor(offset, state_dir=None):
    path = _outcome_cursor_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            json.dump({"offset": offset}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def _reconcile_outcomes(now_ts=None, state_dir=None):
    """Turn every worker_done row the ledger has grown since the last pass into a completion
    record for the job that finished, IF that job is still sitting at "dispatched" or
    "awaiting_fleet" in done/. Returns the list of completion records written.

    Cursor-based (see OUTCOME_CURSOR_FILE above). A torn tail -- a line with no trailing
    newline yet, because another process is mid-write -- stops the scan for this pass without
    advancing past it; the next pass picks up from the same byte and reads it whole once the
    writer finishes. Idempotent: a jid that already has a done/<jid>.outcome.json is skipped,
    so replaying the same cursor twice (a crash between advancing it and finishing this
    function) cannot double-write.
    """
    out = []
    ensure_dirs()
    now = time.time()
    path = _socket_route_path(state_dir)
    offset = _read_outcome_cursor(state_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return out                     # no ledger yet -- nothing to reconcile
    if offset > size:
        offset = 0                     # the ledger was rotated/replaced under us
    new_offset = offset
    rows = []
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break               # torn tail -- leave it for the next pass
                new_offset += len(raw)
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("event") == "worker_done" and row.get("jid"):
                    rows.append(row)
    except OSError:
        return out
    _write_outcome_cursor(new_offset, state_dir)

    for row in rows:
        jid = row.get("jid")
        base_path = _p("done", "%s.json" % jid)
        outcome_path = _p("done", "%s.outcome.json" % jid)
        if os.path.isfile(outcome_path):
            continue
        try:
            with open(base_path, encoding="utf-8") as fh:
                base_rec = json.load(fh)
        except Exception:
            continue                   # no dispatch record for this jid (a bare -g goal, an
                                        # interactive retry) -- nothing here to reconcile
        if base_rec.get("status") not in ("dispatched", "awaiting_fleet"):
            continue
        norm = _WORKER_STATUS_TO_JOB_STATUS.get(row.get("status"), "unknown")
        rec = {"id": jid, "type": "fleet_goal", "destination": "fleet",
               "ts_done": now_ts if now_ts is not None else now,
               "status": norm,
               "result": {"worker": row.get("worker"), "outcome": row.get("outcome"),
                          "turns": row.get("turns"), "reason": row.get("reason"),
                          "route": row.get("route"), "worker_status": row.get("status"),
                          "ledger_ts": row.get("ts")},
               "error": None}
        try:
            with open(outcome_path, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=2)
        except OSError:
            continue
        out.append(rec)
    return out


#: How long a fleet-bound job may sit at "dispatched"/"awaiting_fleet" with no landing ack, no
#: recorded outcome, and no matching live worker before job_status() stops repeating that word
#: and says plainly that nothing more is known. Generous: autostart's own grace is measured in
#: minutes, and reporting "unknown" too early would just be a second word for the silence this
#: function exists to replace.
JOB_STATUS_UNKNOWN_AFTER_S = float(
    os.environ.get("TASK_JOB_STATUS_UNKNOWN_AFTER_S", "1800") or 1800)


def _jid_is_live_worker(jid, state_dir=None):
    """Is `jid` a worker the CURRENT live run is actually holding right now? Reads status.json
    with the same liveness rule fleet_is_live uses, so a stale snapshot from a run that has
    already died cannot be read as "still in flight"."""
    if not jid:
        return False
    sd = state_dir or FLEET_STATE_DIR
    try:
        sp = os.path.join(sd, "status.json")
        if not os.path.isfile(sp) or (time.time() - os.path.getmtime(sp)) > FLEET_LIVE_MAX_AGE_S:
            return False
        with open(sp, encoding="utf-8-sig") as fh:
            snap = json.load(fh) or {}
        if not snap.get("running"):
            return False
        for w in snap.get("workers") or []:
            if isinstance(w, dict) and w.get("jid") == jid:
                return True
    except Exception:
        pass
    return False


def job_status(jid, state_dir=None):
    """Answer "did this job finish, and how", for one admitted goal, without the caller needing
    to know that done/<jid>.json records dispatch rather than completion, or that the real
    answer might be sitting in a different file entirely. Read-only: never writes, so it is
    always safe to call against a live queue.

    Returns {id, state, status, detail, ts_done, result}. `state` is one of:
      "finished"   a worker_done row for this jid exists (already reconciled into
                   done/<jid>.outcome.json, or found live if that pass has not run yet).
                   `status` is the normalized outcome (done/cancelled/error/stuck).
      "in_flight"  no outcome yet, but the jid is a worker in the CURRENT live run right now --
                   it has not finished, and this function can plainly see it has not been
                   lost either.
      "unknown"    the base record says "dispatched"/"awaiting_fleet", nothing else has ever
                   been recorded for it, it is not a live worker, and either no fleet is
                   running now or the wait has outlasted JOB_STATUS_UNKNOWN_AFTER_S. This is
                   the case this module exists to stop mis-stating: a goal dispatched into a
                   run that was later stopped, or one whose completion predates jid reaching
                   the ledger, no longer reads as though it were quietly still in progress --
                   it reads as exactly what is true, which is that nothing further is known.
      "refused"    the fleet command channel READ this goal's add_goal command and REFUSED to
                   apply it (validate_command / admit_command, e822fb6). `result.errors` names
                   why. Definitive and immediate -- unlike "unknown" this never waits out
                   JOB_STATUS_UNKNOWN_AFTER_S, because a refused command produces no worker and
                   nothing will ever arrive later to say more.
      "not_found"  no done/ record exists for this jid at all.
      (anything else) the job's own recorded terminal status, unchanged -- local jobs
                   (ok/error/denied/awaiting_approval) and CLAUDE escalations already resolve
                   through their own path and are not the gap this function closes.
    """
    ensure_dirs()
    outcome_path = _p("done", "%s.outcome.json" % jid)
    if os.path.isfile(outcome_path):
        try:
            with open(outcome_path, encoding="utf-8") as fh:
                rec = json.load(fh)
            # A REFUSED COMMAND IS WRITTEN TO THIS SAME PATH (see _reconcile_landings), not
            # because a worker finished -- none ever ran -- but because this is the one place
            # job_status already treats as terminal-and-checked-first, and re-deriving that
            # here as a second file to check would risk missing it the way "landed"/
            # "reconcile-requeued" records already are (see the "unknown" wording above: those
            # are supplementary, base_path stays authoritative for them). `detail` is carried
            # from the record when the writer set one (the refusal does); worker-outcome
            # records never set it, so they keep the ledger wording unchanged.
            state = "refused" if rec.get("status") == "refused" else "finished"
            return {"id": jid, "state": state, "status": rec.get("status"),
                    "detail": rec.get("detail") or "completion recorded by the fleet's own ledger",
                    "ts_done": rec.get("ts_done"), "result": rec.get("result")}
        except Exception:
            pass

    base_path = _p("done", "%s.json" % jid)
    if not os.path.isfile(base_path):
        return {"id": jid, "state": "not_found", "status": None,
                "detail": "no done/ record exists for this id", "ts_done": None, "result": None}
    try:
        with open(base_path, encoding="utf-8") as fh:
            base_rec = json.load(fh)
    except Exception:
        base_rec = {}

    if base_rec.get("status") not in ("dispatched", "awaiting_fleet"):
        return {"id": jid, "state": base_rec.get("status"), "status": base_rec.get("status"),
                "detail": "terminal status recorded at dispatch time",
                "ts_done": base_rec.get("ts_done"), "result": base_rec.get("result")}

    # A completion that landed in the ledger since the last _reconcile_outcomes tick.
    row = _find_worker_outcome_by_jid(jid, state_dir)
    if row is not None:
        norm = _WORKER_STATUS_TO_JOB_STATUS.get(row.get("status"), "unknown")
        return {"id": jid, "state": "finished", "status": norm,
                "detail": "completion recorded by the fleet's own ledger",
                "ts_done": row.get("ts"),
                "result": {"worker": row.get("worker"), "outcome": row.get("outcome"),
                          "turns": row.get("turns"), "reason": row.get("reason"),
                          "route": row.get("route"), "worker_status": row.get("status")}}

    if _jid_is_live_worker(jid, state_dir):
        return {"id": jid, "state": "in_flight", "status": base_rec.get("status"),
                "detail": "still an active worker in the current live run",
                "ts_done": None, "result": None}

    try:
        dispatched_ts = os.path.getmtime(base_path)
    except OSError:
        dispatched_ts = 0
    age = time.time() - dispatched_ts
    live_now = fleet_is_live(state_dir)
    if (not live_now) or age > JOB_STATUS_UNKNOWN_AFTER_S:
        why = ("no fleet is running now" if not live_now
               else "it has been waiting %.0fs with no result" % age)
        return {"id": jid, "state": "unknown", "status": base_rec.get("status"),
                "detail": ("no completion was ever recorded for this goal, and %s -- this is "
                           "not a claim that it failed, only that nothing further is known"
                           % why),
                "ts_done": None, "result": base_rec.get("result")}

    return {"id": jid, "state": "dispatched", "status": base_rec.get("status"),
            "detail": "recently dispatched; no completion or live-worker signal yet",
            "ts_done": None, "result": base_rec.get("result")}


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
    # AND `created`, WHICH IS THE OTHER HALF OF EVERY QUESTION ts_done CAN ANSWER. The comment
    # above rescued `origin` and stopped one field short. A done record carrying only ts_done
    # cannot say how long the job waited before anything picked it up -- the difference between
    # a queue that drains and one that does not -- and the pending file holding `created` is
    # deleted at this same moment. Found 2026-09-18 reading a real archived job: its `created`
    # was absent, so it read as 1970-01-01 and the wait was unrecoverable.
    if job.get("created"):
        rec["created"] = job["created"]
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
                    # The gate is keyed on what answering it approves (see _gate_key), and the
                    # question shows that payload in full.
                    key = _gate_key(job_type, payload, _static_risk(job_type, payload)[0])
                    token = _gate_token_for_class(key)
                    question = _job_gate_question(job_type, payload, key)
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
            payload = job.get("payload") or {}
            goal = payload.get("goal") or payload.get("text", "")
            prio = bool(payload.get("priority"))
            rec["status"], rec["result"] = fleet_handoff(goal, jid, priority=prio)
            if rec["status"] != "dispatched":
                # fleet_handoff already parked it on every non-dispatched path, carrying the
                # priority with it; this second write is the belt to that braces and must
                # carry the same field or it would overwrite the parked copy with a poorer one.
                _write_for_fleet(jid, goal, priority=prio)
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
    alive_cache = {}                       # one liveness query per pid per pass
    for name in sorted(os.listdir(_p("pending", ""))):
        if not name.endswith(".json"):
            continue
        src = _p("pending", name)
        claimed = _p("running", name)
        if _still_owned(src, alive_cache):
            continue
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
    # A LAUNCH THAT DIED HAS TO BE NOTICED BY SOMETHING THAT RUNS. Before this line the
    # recovery existed and nothing called it, which is the same as not having written it --
    # the goals of a fleet that failed to come up would sit in a record nobody read. Runs
    # before the delivery pass so restored goals go out on this same sweep rather than the next.
    try:
        recover_failed_autostart(now=now_ts)
    except Exception:
        pass
    # RECONCILE BEFORE DELIVERY. Turn open "dispatched" claims into confirmed landings or
    # re-queued waiters first, so a goal that was lost to the stale-status window is back on
    # the for_fleet/ channel in time for _deliver_waiting_goals to hand it over on this same
    # sweep rather than the next.
    try:
        out.extend(_reconcile_landings(now_ts=now_ts))
    except Exception:
        pass
    # OUTCOME RECONCILE. Independent of the landing pass above: landing says the fleet TOOK a
    # goal, this says what happened to it afterwards. Runs every tick so a completion shows up
    # in done/ within one poll interval of the ledger recording it, rather than only when
    # someone happens to call job_status() for that particular id.
    try:
        out.extend(_reconcile_outcomes(now_ts=now_ts))
    except Exception:
        pass
    out.extend(_deliver_waiting_goals(now_ts=now_ts))
    return out


def _deliver_waiting_goals(now_ts=None, state_dir=None):
    """Hand the fleet the goals that arrived while it was not running.

    THE GAP THIS CLOSES. A fleet-bound goal submitted with no run in flight was filed as
    "awaiting_fleet" and written to done/, which is terminal -- nothing looked at it again.
    The status was the only thing waiting. Six such records had built up, each naming a goal
    that would never be delivered however long a fleet ran afterwards.

    WHY THIS NO LONGER RETURNS EARLY WHEN NOTHING IS LIVE. It used to open with
    `if not fleet_is_live(state_dir): return out` -- the function whose stated job is to
    deliver goals that arrived while no fleet was running gave up precisely when no fleet was
    running. That made for_fleet/ a one-way door. A goal parked here by a FRESH submission had
    already passed through fleet_handoff and could reach autostart, but a goal parked by
    _reconcile_landings' requeue path is written straight into the directory, so once it
    landed here with nothing live, nothing ever looked at it again. Measured 2026-09-09: one
    goal sat in for_fleet/ while the supervisor ran this pass every 15s for 47 minutes and
    logged nothing at all -- silence, not an error, because dispatch_once kept returning [].
    The guard was the very condition it existed to fix.

    fleet_handoff already makes the same liveness check itself, and falls through to AUTOSTART
    when it fails, so the guard bought nothing except the dead end.

    THERE IS NO CLAIM HERE, AND WHAT MAKES THAT SAFE IS SOMEWHERE ELSE. dispatch_once takes
    its work with an atomic rename into running/, so two routers cannot both get the same job.
    This loop only reads the file and delivers, deleting it afterwards, so two routers running
    together WOULD hand the same goal over twice. What prevents that is not in this module:
    scripts/supervisor.ps1:139 holds `Global\\m365-copilot-companion-supervisor` as a
    single-instance mutex and runs this with --once, sequentially. (The doubled backslash is
    the docstring escaping itself -- the mutex name has one. Writing it with one made this
    an invalid escape sequence, which tools/test_a_backslash_in_a_literal_means_what_it_says
    caught on a commit that changed nothing but prose.)

    So the exposure is a second router started BY HAND while the supervisor is up -- which is
    a documented thing to do -- and it is written down here rather than guarded because a
    claim needs a restore path for a delivery that fails and a recovery for one that dies
    mid-claim. That is machinery for a case the mutex already covers, and this repository has
    paid for machinery built ahead of the caller that needed it. An unstated dependency,
    though, is how the console-window defect survived: everything downstream of
    supervisor.ps1 was windowless because it started the tree with -WindowStyle Hidden, and
    nothing said so, so each new launch site inherited safety it did not know it had.

    ONE COLD HANDOFF PER PASS. Removing the guard alone would let N waiting files each attempt
    an autostart within a single pass, and autostart_status cannot deduplicate them: a launch
    takes seconds to become live, so every file in the same pass still reads "nothing in
    flight". N goals would mean N fleets -- trading a stall for an amplification. So when
    nothing is live, exactly one goal is offered per pass; if it starts a fleet, the next pass
    finds it live and delivers the whole backlog by the normal path. AUTOSTART_BACKOFF_S stays
    the outer bound; this only stops one pass from racing itself.
    """
    out = []
    ensure_dirs()
    try:
        names = sorted(os.listdir(_p("for_fleet", "")))
    except OSError:
        return out
    live = fleet_is_live(state_dir)
    offered_cold = False
    for name in names:
        if not name.endswith(".txt"):
            continue
        path = _p("for_fleet", name)
        try:
            with open(path, encoding="utf-8") as fh:
                goal, parked_priority = read_for_fleet(fh.read())
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
        if not live:
            if offered_cold:
                continue                   # one cold start per pass -- see the docstring
            offered_cold = True
        status, result = fleet_handoff(goal, jid, state_dir, priority=parked_priority)
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
        # PROVENANCE, THE SECOND TIME. run_job already rescues `origin` and `created` across
        # the moment a job becomes a record, with the reason written beside it: "a distinction
        # that does not reach the audit trail is not a distinction". This writer is the OTHER
        # place a done-record is built, and it built one from scratch -- so a goal delivered
        # late read origin=null, which says "nobody knows where this came from" about a job
        # whose origin was on disk the whole time.
        #
        # Measured 2026-09-18: cli1789703602_14000_0.delivered.json carried origin=null while
        # cli1789703602_14000_0.json, written by the earlier pass for the SAME id, carried
        # {"via": "cli", "source": "...fleet_runner.py --goals-file ..."}.
        #
        # The pending file is already gone by here -- it is deleted the moment delivery
        # succeeds, a few lines up -- so the earlier done-record is where to look.
        try:
            with open(_p("done", "%s.json" % jid), encoding="utf-8") as fh:
                earlier = json.load(fh) or {}
            for field in ("origin", "created"):
                if earlier.get(field) and not rec.get(field):
                    rec[field] = earlier[field]
        except Exception:
            pass
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
        rec = {"id": jid, "type": job_type, "destination": "local",
               "ts_done": now_ts, "status": None, "result": None, "error": None}
        # EVERYTHING BELOW IS DERIVED FROM THE PAYLOAD, NEVER READ FROM THE FILE. awaiting/ is
        # as writable as pending/, so a job can arrive here without job_gate() ever having
        # seen it. The policy is therefore asked again: a payload the current mode refuses
        # outright is refused here too, rather than being run on some other job's answer.
        decision, why = job_gate(job_type, payload, _current_approval_mode(TASK_JOB_APPROVAL_MODE))
        if decision == "DENY":
            rec["status"], rec["error"] = "denied", why
            try:
                with open(_p("done", name), "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=2)
                os.remove(path)
            except OSError:
                pass
            out.append(rec)
            continue
        if decision == "ALLOW":
            # e.g. the operator switched to `bypass` (or `auto` cleared it) while this job
            # sat in awaiting/ from an earlier, stricter mode. job_gate already logged the
            # bypass decision if that's why; the only thing left is to NOT fall into the
            # gate-lookup/-creation code below, which would raise exactly the question this
            # mode exists to skip -- see the STUCK-unlock incident this whole file's bypass
            # handling was written to close (gates kept arriving after bypass was chosen).
            try:
                fn = LOCAL_EXECUTORS.get(job_type)
                if not fn:
                    rec["status"], rec["error"] = "error", "no local executor for type %r" % job_type
                else:
                    rec["status"], rec["result"], rec["error"] = fn(payload)
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
            continue
        class_key = _job_class_key(job_type, payload)
        key = _gate_key(job_type, payload, _static_risk(job_type, payload)[0])
        token = _gate_token_for_class(key)
        gate = _read_job_gate(token)
        if gate is None:
            # A waiting job that no gate asks about can never be answered: it arrived here
            # directly, or its key changed since it was parked. Ask about it now -- once, since
            # _write_job_gate never overwrites an existing gate.
            _write_job_gate(token, _job_gate_question(job_type, payload, key),
                            "task_router job class: %s" % key)
            continue
        if not gate.get("answered"):
            continue  # still waiting -- no sleep, just move on to the next tick
        answer = str(gate.get("answer") or "").lower().strip()
        try:
            if answer == "approved":
                # Record only what the gate asked about. A gate for a payload the static check
                # flagged approved that payload, once -- not its class, and recording the exact
                # key would change nothing (job_gate never ALLOWs a flagged payload).
                if key == class_key:
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
    ap.add_argument("--status", metavar="JID",
                    help="print job_status() for one admitted job id and exit -- 'did this "
                         "job finish, and how', read-only, without needing to know done/ "
                         "records dispatch rather than completion")
    args = ap.parse_args()
    ensure_dirs()
    if args.status:
        print(json.dumps(job_status(args.status), ensure_ascii=False, indent=2))
        return
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
