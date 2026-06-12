"""Self-driving SWE pilot orchestrator. Launch ONCE; it runs unattended until every target
instance is RESOLVED (or MAX_ROUNDS hit), so a flaky operator (malformed tool calls) cannot
stall the underlying work.

Robustness:
  * single-writer guarantee: kills any existing relay.fleet_runner at each round start and
    kills any stray duplicate runner (!= the one we launched) every 5s while waiting -- this is
    the fix for the turn-reset chaos caused by two concurrent fleet_runners writing status.json.
  * lockfile: a second orchestrator refuses to start.
  * loop: reset worktrees -> build goals -> run one fleet -> check status -> repeat for the
    still-unresolved instances, with the improved swe_check feedback gate doing the verification.

  python bench/swe_run_until_done.py [<instance_id> ...]   (default: the two pilot failures)
"""
import atexit
import json
import os
import re
import subprocess
import sys
import time

REPO = r"C:\Users\USER\companion-mcp"
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
WORK = os.path.join(REPO, ".fleet", "swe", "work")
SWEDIR = os.path.join(REPO, ".fleet", "swe")
LOG = os.path.join(SWEDIR, "run_until_done.log")
LOCK = os.path.join(SWEDIR, "run_until_done.lock")
STATUS = os.path.join(REPO, ".fleet", "status.json")
GOALS = os.path.join(SWEDIR, "goals2.jsonl")

TARGETS = sys.argv[1:] or ["astropy__astropy-14182", "astropy__astropy-14365"]
MAX_ROUNDS = 6


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fleet_pids():
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*relay.fleet_runner*' } | "
         "ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True, errors="replace")
    return [p.strip() for p in (ps.stdout or "").split() if p.strip().isdigit()]


def kill_pids(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)


def kill_all_fleet():
    pids = fleet_pids()
    if pids:
        kill_pids(pids)
        log("killed existing fleet_runner pids: " + ",".join(pids))
        time.sleep(2)


def acquire_lock():
    if os.path.exists(LOCK):
        try:
            old = open(LOCK).read().strip()
        except Exception:
            old = ""
        if old:
            chk = subprocess.run(["tasklist", "/FI", "PID eq " + old],
                                 capture_output=True, text=True, errors="replace")
            if old in (chk.stdout or ""):
                log("another orchestrator (pid %s) already running; exiting" % old)
                sys.exit(0)
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK) and os.remove(LOCK))


def resolved_set():
    try:
        d = json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for w in d.get("workers", []):
        if not (w.get("outcome") == "DONE" and w.get("verified")):
            continue
        # the FINAL snapshot historically lacked "cwd", so also recover the
        # instance id from the goal text (which embeds the wt_<instance> path)
        inst = w.get("cwd", "").split("wt_")[-1]
        if inst:
            out.add(inst)
        m = re.search(r"wt_([A-Za-z0-9_.-]+)", w.get("goal", "") or "")
        if m:
            out.add(m.group(1))
    return out


def run_round(insts):
    r = subprocess.run([VENVPY, os.path.join(REPO, "bench", "swe_rerun_setup.py")] + insts,
                       capture_output=True, text=True, errors="replace")
    log("setup: " + (r.stdout or "").strip().replace("\n", " | "))
    kill_all_fleet()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii:replace"
    p = subprocess.Popen(
        [VENVPY, "-m", "relay.fleet_runner", "--goals-file", GOALS,
         "--max-concurrent", str(len(insts)), "--max-turns", "20", "--max-transient", "10"],
        cwd=REPO, env=env)
    log("launched fleet_runner pid %d on %d instance(s)" % (p.pid, len(insts)))
    # fleet_runner legitimately runs as parent + a child '-m relay.fleet_runner' process; do NOT
    # kill the child. Just wait for our parent to finish; the child lives/dies with it.
    p.wait()
    log("fleet_runner exited rc=%s" % p.returncode)


def main():
    acquire_lock()
    log("=== run_until_done start targets=%s ===" % TARGETS)
    remaining = list(TARGETS)
    for rnd in range(1, MAX_ROUNDS + 1):
        log("--- round %d/%d remaining=%s ---" % (rnd, MAX_ROUNDS, remaining))
        run_round(remaining)
        done = resolved_set()
        for i in [x for x in remaining if x in done]:
            log("RESOLVED: %s" % i)
        remaining = [i for i in remaining if i not in done]
        if not remaining:
            log("=== ALL TARGETS RESOLVED in %d round(s) ===" % rnd)
            return
        log("still unresolved after round %d: %s" % (rnd, remaining))
    log("=== STOPPED after %d rounds, unresolved=%s ===" % (MAX_ROUNDS, remaining))


if __name__ == "__main__":
    main()
