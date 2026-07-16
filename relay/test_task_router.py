"""Tests for the typed-job task router: destination routing + LOCAL executors + end-to-end queue
+ the 3-mode job-approval gate (default/auto/bypass)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay.task_router as tr

def check(name, cond):
    """Print a PASS/FAIL line (handy for the standalone `python relay/test_task_router.py`
    run) and raise a real AssertionError on failure so pytest sees it as a normal failing
    test rather than a silently-swallowed bookkeeping entry."""
    print(("PASS " if cond else "FAIL ") + name)
    assert cond, name


NOTIFY_CALLS = []


def _stub_notify_approval_gate(*args, **kwargs):
    """Stand-in for tr.notify_approval_gate -- captures calls instead of firing a real Windows
    desktop toast. The CONFIRM path of job_gate/dispatch (see test_dispatch_awaiting_gate_flow)
    calls tr.notify_desktop() directly, and without this stub every test run popped a real
    OS notification with the test fixture payload. Mirrors the discipline test_admission.py
    already applies to rf.default_notify."""
    NOTIFY_CALLS.append((args, kwargs))


def _use_tmp_tasks():
    """Fresh isolated tempdir for TASKS + the approved-jobs store + the gate directory (via
    ALLOWED_BASE), so tests never touch the real repo's .fleet/ or the real user's
    .companion_gates/. Mode defaults to "bypass" here because most of these tests exercise
    the executors directly -- the approval-gate tests below override the mode explicitly."""
    d = tempfile.mkdtemp(prefix="tasks_test_")
    tr.TASKS = d
    tr.APPROVED_JOBS_FILE = os.path.join(d, "approved_jobs.json")
    tr.ALLOWED_BASE = Path(d)
    tr.TASK_JOB_APPROVAL_MODE = "bypass"
    tr.ensure_dirs()
    # Stub the real desktop-toast side effect for every test path -- never let the actual
    # OS notification fire during a test run.
    tr.notify_approval_gate = _stub_notify_approval_gate
    return d


def test_destination_routing():
    check("shell->local", tr.destination_for({"type": "shell"}) == "local")
    check("screenshot->local", tr.destination_for({"type": "screenshot"}) == "local")
    check("coding->fleet", tr.destination_for({"type": "coding"}) == "fleet")
    check("research->fleet", tr.destination_for({"type": "research"}) == "fleet")
    check("deepresearch->claude", tr.destination_for({"type": "deep-research"}) == "claude")
    check("unknown->claude", tr.destination_for({"type": "wat"}) == "claude")
    # explicit escalate forces CLAUDE even for a normally-local type
    check("escalate-forces-claude",
          tr.destination_for({"type": "shell", "payload": {"escalate": True}}) == "claude")


def test_local_shell_and_python():
    _use_tmp_tasks()
    rec = tr.run_job({"id": "t1", "type": "shell", "payload": {"cmd": "echo hello-router"}})
    check("shell ok", rec["status"] == "ok")
    check("shell output", "hello-router" in (rec["result"] or {}).get("output", ""))
    rec = tr.run_job({"id": "t2", "type": "python", "payload": {"code": "print(6*7)"}})
    check("python ok", rec["status"] == "ok")
    check("python output", "42" in (rec["result"] or {}).get("output", ""))
    # a failing shell becomes status=error, not an exception
    rec = tr.run_job({"id": "t3", "type": "shell", "payload": {"cmd": "exit 3"}})
    check("shell failure -> error status", rec["status"] == "error")
    # missing payload -> graceful error
    rec = tr.run_job({"id": "t4", "type": "shell", "payload": {}})
    check("missing cmd -> error", rec["status"] == "error" and rec["error"])


def test_file_roundtrip():
    # tempdir d sits outside REPO -- the H3 hard floor (see test_h3_file_path_floor below)
    # would reject it by default, so these executor-correctness tests opt in explicitly.
    d = _use_tmp_tasks()
    fp = os.path.join(d, "rt.txt")
    rec = tr.run_job({"id": "w", "type": "file",
                       "payload": {"op": "write", "path": fp, "content": "xyz", "allow_outside": True}})
    check("file write ok", rec["status"] == "ok")
    rec = tr.run_job({"id": "r", "type": "file",
                       "payload": {"op": "read", "path": fp, "allow_outside": True}})
    check("file read ok", rec["status"] == "ok" and rec["result"]["content"] == "xyz")


def test_fleet_and_claude_handoff():
    d = _use_tmp_tasks()
    rec = tr.run_job({"id": "c1", "type": "coding", "payload": {"goal": "fix the bug"}})
    check("coding dispatched", rec["status"] == "dispatched")
    check("fleet handoff written", os.path.isfile(os.path.join(d, "for_fleet", "c1.txt")))
    rec = tr.run_job({"id": "d1", "type": "deep-research", "payload": {"q": "x"}})
    check("deepresearch escalated", rec["status"] == "escalated")
    check("claude handoff written", os.path.isfile(os.path.join(d, "for_claude", "d1.json")))


def test_dispatch_once_endtoend():
    d = _use_tmp_tasks()
    job = {"id": "e2e", "type": "python", "payload": {"code": "print('done-e2e')"}}
    with open(os.path.join(d, "pending", "e2e.json"), "w", encoding="utf-8") as f:
        json.dump(job, f)
    recs = tr.dispatch_once()
    check("dispatched one", len(recs) == 1 and recs[0]["status"] == "ok")
    check("done file written", os.path.isfile(os.path.join(d, "done", "e2e.json")))
    check("pending drained", not os.path.isfile(os.path.join(d, "pending", "e2e.json")))


# ── Job-approval gate tests ─────────────────────────────────────────────────────────────────

def test_job_class_key():
    check("git status vs git push --force are DISTINCT classes",
          tr._job_class_key("shell", {"cmd": "git status"}) !=
          tr._job_class_key("shell", {"cmd": "git push --force"}))
    check("shell class whitespace-normalized",
          tr._job_class_key("shell", {"cmd": "git   status"}) ==
          tr._job_class_key("shell", {"cmd": "git status"}))
    check("shell class keys on first two tokens only",
          tr._job_class_key("shell", {"cmd": "git status --short"}) ==
          tr._job_class_key("shell", {"cmd": "git status --long --extra"}))
    check("python class keys on first two tokens",
          tr._job_class_key("python", {"code": "import os"}) ==
          tr._job_class_key("python", {"code": "import os\nprint(1)"}))
    check("python class distinguishes different first tokens",
          tr._job_class_key("python", {"code": "import os"}) !=
          tr._job_class_key("python", {"code": "os.remove('x')"}))
    d = _use_tmp_tasks()
    p1 = os.path.join(d, "sub", "a.txt")
    p2 = os.path.join(d, "sub", "b.txt")
    p3 = os.path.join(d, "other", "c.txt")
    check("file class keys on (op, parent dir) -- same parent -> same class",
          tr._job_class_key("file", {"op": "write", "path": p1}) ==
          tr._job_class_key("file", {"op": "write", "path": p2}))
    check("file class differs across parent dirs",
          tr._job_class_key("file", {"op": "write", "path": p1}) !=
          tr._job_class_key("file", {"op": "write", "path": p3}))
    check("file class differs across ops on the same dir",
          tr._job_class_key("file", {"op": "write", "path": p1}) !=
          tr._job_class_key("file", {"op": "read", "path": p1}))


def test_approved_jobs_store():
    _use_tmp_tasks()
    key = "shell::echo hi"
    check("nothing approved initially", not tr._is_class_approved(key))
    check("empty store round-trips", tr._load_approved_jobs() == {"classes": {}})
    tr._approve_class(key, example="echo hi")
    check("approved after add", tr._is_class_approved(key))
    check("unrelated class still not approved", not tr._is_class_approved("shell::rm -rf"))
    # corrupt file -> tolerated, treated as empty (never raises)
    with open(tr.APPROVED_JOBS_FILE, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    check("corrupt store tolerated as empty", tr._load_approved_jobs() == {"classes": {}})
    check("corrupt store -> lookup says not-approved", not tr._is_class_approved(key))
    # missing file entirely -> also tolerated
    os.remove(tr.APPROVED_JOBS_FILE)
    check("missing store tolerated as empty", tr._load_approved_jobs() == {"classes": {}})


def test_job_gate_modes():
    _use_tmp_tasks()
    clean_shell = {"cmd": "git status"}
    destructive_shell = {"cmd": "rm -rf /some/dir"}
    ask_shell = {"cmd": "git push origin main"}

    # bypass: ALLOW unconditionally, regardless of content (H3 path floor is separate/always-on)
    check("bypass allows clean", tr.job_gate("shell", clean_shell, "bypass")[0] == "ALLOW")
    check("bypass allows destructive too",
          tr.job_gate("shell", destructive_shell, "bypass")[0] == "ALLOW")

    # auto: purely static check, never executes the payload to test it
    check("auto: clean -> ALLOW", tr.job_gate("shell", clean_shell, "auto")[0] == "ALLOW")
    check("auto: destructive -> DENY", tr.job_gate("shell", destructive_shell, "auto")[0] == "DENY")
    check("auto: ask-pattern -> CONFIRM", tr.job_gate("shell", ask_shell, "auto")[0] == "CONFIRM")

    # default: first occurrence of ANY new class always confirms, clean or not
    check("default: first-seen clean -> CONFIRM",
          tr.job_gate("shell", clean_shell, "default")[0] == "CONFIRM")
    check("default: first-seen destructive -> CONFIRM",
          tr.job_gate("shell", destructive_shell, "default")[0] == "CONFIRM")
    check("default: first-seen ask -> CONFIRM",
          tr.job_gate("shell", ask_shell, "default")[0] == "CONFIRM")

    # default: once the class is approved AND the payload is clean -> ALLOW
    key = tr._job_class_key("shell", clean_shell)
    tr._approve_class(key)
    check("default: approved + clean -> ALLOW",
          tr.job_gate("shell", clean_shell, "default")[0] == "ALLOW")

    # THE MITIGATION: approving a class does NOT let a destructive payload of that
    # SAME class auto-run -- job_gate re-checks the static risk on every call.
    destructive_same_class_text = {"cmd": "git status; rm -rf /"}
    key2 = tr._job_class_key("shell", destructive_same_class_text)
    tr._approve_class(key2)
    check("class is recorded approved", tr._is_class_approved(key2))
    decision, reason = tr.job_gate("shell", destructive_same_class_text, "default")
    check("approved-but-destructive -> CONFIRM, never ALLOW", decision == "CONFIRM")
    check("mitigation reason mentions risky payload", "risky" in reason)


def test_h3_file_path_floor():
    _use_tmp_tasks()
    # a path clearly outside REPO (and outside any TASK_FILE_ALLOWED_BASE) must be rejected,
    # and the write must NOT actually happen.
    outside = os.path.join(tempfile.gettempdir(), "task_router_h3_probe_%d.txt" % os.getpid())
    if os.path.isfile(outside):
        os.remove(outside)
    status, result, err = tr._exec_file({"op": "write", "path": outside, "content": "should not land"})
    check("outside-repo path rejected", status == "error")
    check("rejection reason mentions the allowed-root floor",
          bool(err) and "outside" in err.lower())
    check("file was NOT actually written", not os.path.isfile(outside))
    # a traversal-style relative path that resolves outside REPO is rejected the same way
    traversal = os.path.join(tr.REPO, "..", "..", "task_router_h3_traversal_probe.txt")
    status2, _result2, err2 = tr._exec_file({"op": "write", "path": traversal, "content": "no"})
    check("traversal path rejected", status2 == "error")
    check("traversal target NOT written",
          not os.path.isfile(os.path.abspath(traversal)))
    # run_job never raises even when the gate rejects the path
    rec = tr.run_job({"id": "h3", "type": "file",
                       "payload": {"op": "write", "path": outside, "content": "no"}})
    check("run_job surfaces the rejection as status=error, not an exception", rec["status"] == "error")
    check("still not written via run_job", not os.path.isfile(outside))


def test_dispatch_awaiting_gate_flow():
    """End-to-end: default mode holds a first-seen shell job in awaiting/ with an unanswered
    gate file (never touching done/ or running it); simulate a human approval by writing the
    same gate-file shape the cockpit writes; a second dispatch_once() then completes it. A
    THIRD dispatch of the same class then auto-runs without a new gate."""
    d = _use_tmp_tasks()
    tr.TASK_JOB_APPROVAL_MODE = "default"

    job = {"id": "gate1", "type": "shell", "payload": {"cmd": "echo gated-run"}}
    with open(os.path.join(d, "pending", "gate1.json"), "w", encoding="utf-8") as f:
        json.dump(job, f)

    recs = tr.dispatch_once()
    check("first dispatch -> awaiting_approval, not ok", len(recs) == 1 and
          recs[0]["status"] == "awaiting_approval")
    check("job moved to awaiting/, not done/",
          os.path.isfile(os.path.join(d, "awaiting", "gate1.json")))
    check("no done/ file yet", not os.path.isfile(os.path.join(d, "done", "gate1.json")))
    check("pending/running drained (non-blocking, no leftover claim)",
          not os.path.isfile(os.path.join(d, "pending", "gate1.json")) and
          not os.path.isfile(os.path.join(d, "running", "gate1.json")))

    token = recs[0]["result"]["gate_token"]
    gate_path = tr.ALLOWED_BASE / ".companion_gates" / ("%s.json" % token)
    check("gate file was created", gate_path.is_file())
    gate_data = json.loads(gate_path.read_text(encoding="utf-8"))
    check("gate starts unanswered", gate_data.get("answered") is False)

    # a second dispatch_once with the gate still unanswered must NOT run the job and must
    # NOT block -- it just leaves it in awaiting/
    recs_wait = tr.dispatch_once()
    check("still-unanswered gate -> job stays in awaiting/, no done/ write",
          os.path.isfile(os.path.join(d, "awaiting", "gate1.json")) and
          not os.path.isfile(os.path.join(d, "done", "gate1.json")))

    # simulate the human/cockpit approving (same file shape, atomic-ish overwrite is fine here)
    gate_data["answered"] = True
    gate_data["answer"] = "approved"
    gate_path.write_text(json.dumps(gate_data, ensure_ascii=False), encoding="utf-8")

    recs2 = tr.dispatch_once()
    done_recs = [r for r in recs2 if r.get("id") == "gate1"]
    check("approved gate -> job now completes", len(done_recs) == 1 and done_recs[0]["status"] == "ok")
    check("done/ file now written", os.path.isfile(os.path.join(d, "done", "gate1.json")))
    check("awaiting/ file removed after completion",
          not os.path.isfile(os.path.join(d, "awaiting", "gate1.json")))
    check("class was recorded as approved",
          tr._is_class_approved(tr._job_class_key("shell", {"cmd": "echo gated-run", "id": "gate1"})))

    # a second job of the SAME class arrives -- must auto-run without a new gate
    job2 = {"id": "gate2", "type": "shell", "payload": {"cmd": "echo gated-run"}}
    with open(os.path.join(d, "pending", "gate2.json"), "w", encoding="utf-8") as f:
        json.dump(job2, f)
    recs3 = tr.dispatch_once()
    same_class_recs = [r for r in recs3 if r.get("id") == "gate2"]
    check("same-class job auto-runs after approval",
          len(same_class_recs) == 1 and same_class_recs[0]["status"] == "ok")
    check("same-class job went straight to done/, no new awaiting file",
          os.path.isfile(os.path.join(d, "done", "gate2.json")))


if __name__ == "__main__":
    # Manual/standalone iteration path only -- pytest never executes this block, because
    # pytest imports the module (running only top-level code, not this __main__ guard) and
    # then calls each test_* function itself, independently, during its own run phase. That
    # eager-on-import hazard (this used to read `if __name__ == "__main__" or True:`) is
    # exactly what turned a normal failing check into a pytest INTERNALERROR during
    # collection -- the `or True` ran the whole suite as a side effect of importing the
    # module, and a failing check()'s sys.exit(1) killed the collector mid-import.
    test_destination_routing()
    test_local_shell_and_python()
    test_file_roundtrip()
    test_fleet_and_claude_handoff()
    test_dispatch_once_endtoend()
    test_job_class_key()
    test_approved_jobs_store()
    test_job_gate_modes()
    test_h3_file_path_floor()
    test_dispatch_awaiting_gate_flow()
    print("\nALL PASS")
