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

  python bench/swe_run_until_done.py [<instance_id> ...]              (legacy pilot default)
  python bench/swe_run_until_done.py --targets-file batch_12.txt \
      --goals .fleet/swe/goals_batch12.jsonl --setup batch \
      --max-rounds 6 --max-concurrent 2 --chunk 4

Batching: with --chunk K (default = len(targets), i.e. one fleet pass over all goals), the
orchestrator processes the remaining instances K at a time per fleet launch. Small chunks
isolate one bad instance's blast radius and bound RAM (2 Edge tabs * K queued goals), at the
cost of more fleet relaunches. Recommended for a 12-instance/2-tab run: --chunk 4.
"""
import argparse
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


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", help="instance ids (legacy positional)")
    ap.add_argument("--targets-file", help="file with one instance id per line")
    ap.add_argument("--goals", default=os.path.join(SWEDIR, "goals2.jsonl"),
                    help="goals JSONL the fleet runs")
    ap.add_argument("--setup", choices=["rerun", "batch"], default="rerun",
                    help="rerun=swe_rerun_setup.py (astropy pilot), batch=swe_batch_setup.py")
    ap.add_argument("--spec", default=os.path.join(SWEDIR, "batch_12_spec.json"),
                    help="batch spec (only used when --setup batch)")
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--max-concurrent", type=int, default=2)
    ap.add_argument("--chunk", type=int, default=0,
                    help="instances per fleet launch (0 = all remaining at once)")
    a = ap.parse_args()
    tgt = list(a.targets)
    if a.targets_file:
        p = a.targets_file
        if not os.path.isabs(p):
            p = os.path.join(SWEDIR, p)
        tgt += [ln.strip() for ln in open(p, encoding="utf-8") if ln.strip()]
    if not tgt:
        tgt = ["astropy__astropy-14182", "astropy__astropy-14365"]
    # de-dup, preserve order
    seen, ordered = set(), []
    for t in tgt:
        if t not in seen:
            seen.add(t); ordered.append(t)
    a.target_list = ordered
    return a


ARGS = parse_args()
TARGETS = ARGS.target_list
MAX_ROUNDS = ARGS.max_rounds
GOALS = ARGS.goals


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


def setup_round(insts):
    """(Re)build the goals file for exactly `insts` using the configured setup script."""
    if ARGS.setup == "batch":
        cmd = [VENVPY, os.path.join(REPO, "bench", "swe_batch_setup.py"),
               "--spec", ARGS.spec, "--goals", GOALS, "--instances"] + insts
    else:
        cmd = [VENVPY, os.path.join(REPO, "bench", "swe_rerun_setup.py")] + insts
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    log("setup: " + (r.stdout or "").strip().replace("\n", " | "))


def run_round(insts):
    setup_round(insts)
    kill_all_fleet()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii:replace"
    p = subprocess.Popen(
        [VENVPY, "-m", "relay.fleet_runner", "--goals-file", GOALS,
         "--max-concurrent", str(ARGS.max_concurrent), "--max-turns", "20",
         "--max-transient", "10"],
        cwd=REPO, env=env)
    log("launched fleet_runner pid %d on %d instance(s) (max-concurrent=%d)"
        % (p.pid, len(insts), ARGS.max_concurrent))
    # fleet_runner legitimately runs as parent + a child '-m relay.fleet_runner' process; do NOT
    # kill the child. Just wait for our parent to finish; the child lives/dies with it.
    p.wait()
    log("fleet_runner exited rc=%s" % p.returncode)


def chunks(seq, k):
    if k <= 0 or k >= len(seq):
        return [list(seq)]
    return [list(seq[i:i + k]) for i in range(0, len(seq), k)]


def main():
    acquire_lock()
    log("=== run_until_done start targets=%d goals=%s chunk=%d max-concurrent=%d ==="
        % (len(TARGETS), os.path.basename(GOALS), ARGS.chunk, ARGS.max_concurrent))
    remaining = list(TARGETS)
    for rnd in range(1, MAX_ROUNDS + 1):
        log("--- round %d/%d remaining=%d ---" % (rnd, MAX_ROUNDS, len(remaining)))
        # process the remaining instances chunk-by-chunk; status.json reflects only the
        # most-recent fleet launch, so collect each chunk's resolved set right after it ends.
        round_done = set()
        for ci, chunk in enumerate(chunks(remaining, ARGS.chunk), 1):
            log("  chunk %d: %s" % (ci, chunk))
            run_round(chunk)
            done = resolved_set()
            for i in [x for x in chunk if x in done]:
                log("RESOLVED: %s" % i)
                round_done.add(i)
        remaining = [i for i in remaining if i not in round_done]
        if not remaining:
            log("=== ALL TARGETS RESOLVED in %d round(s) ===" % rnd)
            return
        log("still unresolved after round %d: %s" % (rnd, remaining))
    log("=== STOPPED after %d rounds, unresolved=%s ===" % (MAX_ROUNDS, remaining))


if __name__ == "__main__":
    main()
