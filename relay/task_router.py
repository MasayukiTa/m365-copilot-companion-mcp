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
  done/<id>.json      finished                {..., status, result, error, ts_done}
  for_fleet/<id>.txt  FLEET handoff (a goal the relay fleet consumes)
  for_claude/<id>.json CLAUDE handoff (escalation the agent picks up)

Design notes:
  * One writer claims a job by moving pending/ -> running/ (atomic rename) so two routers never
    double-run a job.
  * LOCAL executors are bounded (timeout) and never raise out of run_job -- a failure becomes a
    {status:"error"} result, so one bad job can't wedge the loop.
  * FLEET/CLAUDE jobs are not executed here; they're written as a handoff artifact and marked
    {status:"dispatched"} -- the fleet runner / Claude agent completes them and writes done/.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS = os.path.join(REPO, ".fleet", "tasks")
SUBDIRS = ("pending", "running", "done", "for_fleet", "for_claude")
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")

# type -> destination. Unknown types default to CLAUDE (the most capable fallback).
DESTINATION = {
    "shell": "local",
    "python": "local",
    "screenshot": "local",
    "file": "local",
    "coding": "fleet",
    "research": "fleet",        # ordinary web research -> M365 (it has web access)
    "deep-research": "claude",  # rigorous, cited, adversarially-verified -> the skill
}
DEFAULT_DESTINATION = "claude"

# A job may force its destination with payload {"escalate": true} -> CLAUDE, regardless of type.
LOCAL_TIMEOUT_S = int(os.environ.get("TASK_LOCAL_TIMEOUT_S", "120"))


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


def _exec_screenshot(payload):
    """Capture the screen to a PNG via PowerShell + .NET (no MCP unlock needed). payload may set
    'out' (path) and 'region' [l,t,w,h]; default full virtual screen to .fleet/tasks/shots/."""
    out = payload.get("out") or _p("done", "shot_%s.png" % payload.get("id", "x"))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    region = payload.get("region")  # [left, top, width, height] or None
    if region and len(region) == 4:
        l, t, w, h = region
        bounds = "New-Object Drawing.Rectangle(%d,%d,%d,%d)" % (l, t, w, h)
    else:
        bounds = ("[System.Windows.Forms.SystemInformation]::VirtualScreen")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b = %s; "
        "$bmp = New-Object Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $b.Size); "
        "$bmp.Save('%s'); $g.Dispose(); $bmp.Dispose()" % (bounds, out.replace("\\", "\\\\"))
    )
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=LOCAL_TIMEOUT_S)
    if r.returncode == 0 and os.path.isfile(out):
        return "ok", {"path": out}, None
    return "error", None, (r.stderr or "screenshot failed")[:2000]


def _exec_file(payload):
    """Read or write a file. payload {op:'read'|'write', path, content?}."""
    op = payload.get("op")
    path = payload.get("path")
    if not path:
        return "error", None, "file job missing 'path'"
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


def run_job(job, now_ts=None):
    """Execute (LOCAL) or hand off (FLEET/CLAUDE) a single job. Returns the done-record dict.
    Never raises -- any failure is captured as status 'error'."""
    jid = job.get("id", "noid")
    dest = destination_for(job)
    rec = {"id": jid, "type": job.get("type"), "destination": dest,
           "ts_done": now_ts, "status": None, "result": None, "error": None}
    try:
        if dest == "local":
            fn = LOCAL_EXECUTORS.get(job.get("type"))
            if not fn:
                rec["status"], rec["error"] = "error", "no local executor for type %r" % job.get("type")
            else:
                payload = dict(job.get("payload") or {}, id=jid)
                rec["status"], rec["result"], rec["error"] = fn(payload)
        elif dest == "fleet":
            # write a goal the relay fleet consumes (one goal per line / file)
            goal = (job.get("payload") or {}).get("goal") or (job.get("payload") or {}).get("text", "")
            with open(_p("for_fleet", "%s.txt" % jid), "w", encoding="utf-8") as f:
                f.write(goal)
            rec["status"], rec["result"] = "dispatched", {"handoff": "for_fleet/%s.txt" % jid}
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
    """Claim and process every pending job. Returns the list of done-records produced."""
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
        with open(_p("done", name), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.remove(claimed)
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
    import time
    print("task_router: polling %s every %ss" % (_p("pending", ""), args.poll_s))
    while True:
        for rec in dispatch_once():
            print("[task] %s/%s -> %s/%s" % (rec.get("type"), rec.get("id"),
                                             rec.get("destination"), rec.get("status")))
        time.sleep(args.poll_s)


if __name__ == "__main__":
    main()
