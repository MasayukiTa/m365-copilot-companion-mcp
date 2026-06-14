"""Tests for the typed-job task router: destination routing + LOCAL executors + end-to-end queue."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay.task_router as tr

_fails = []


def check(name, cond):
    if not cond:
        _fails.append(name)
    print(("PASS " if cond else "FAIL ") + name)


def _use_tmp_tasks():
    d = tempfile.mkdtemp(prefix="tasks_test_")
    tr.TASKS = d
    tr.ensure_dirs()
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
    d = _use_tmp_tasks()
    fp = os.path.join(d, "rt.txt")
    rec = tr.run_job({"id": "w", "type": "file", "payload": {"op": "write", "path": fp, "content": "xyz"}})
    check("file write ok", rec["status"] == "ok")
    rec = tr.run_job({"id": "r", "type": "file", "payload": {"op": "read", "path": fp}})
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


if __name__ == "__main__" or True:
    test_destination_routing()
    test_local_shell_and_python()
    test_file_roundtrip()
    test_fleet_and_claude_handoff()
    test_dispatch_once_endtoend()
    print("\n%s" % ("ALL PASS" if not _fails else "FAILURES: " + ", ".join(_fails)))
    if _fails:
        sys.exit(1)
