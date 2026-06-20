"""DECOUPLED SWE-bench solve orchestrator (solve locally, grade later on kiyus).

Phase 1 of the trustworthy pass@1 run. Solves a target set (default: the full Lite 300)
with the local Copilot fleet and CAPTURES each instance's diff -- but runs NO local Docker
eval (that is the 16 GB box's hardware wall). Correctness during solve is the agent's own
job via the strong-scaffold red->green self-test; the hidden tests are applied ONCE, later,
on the kiyus eval host (bench/swe_grade_batch.py).

Disk-safe by construction: worktrees are staged in CHUNK-sized waves (default 25 ~= <=1.5 GB
of light checkouts at a time), solved, their diffs captured, then the worktrees are RELEASED
(the shared per-repo blobless clones stay warm). So C: never holds all 300 worktrees at once
(300 * ~36 MB ~= 11 GB would breach the floor on a 12 GB-free disk).

Resumable: an instance whose diff is already captured in preds_solve/<inst>.json is skipped,
so a crash / reboot / network blip just continues. Single-instance lock.

  python bench/swe_solve_decoupled.py                      # full 300 (strong scaffold)
  python bench/swe_solve_decoupled.py --targets-file _all300.txt --chunk 25 --max-concurrent 2
  python bench/swe_solve_decoupled.py --baseline           # scaffold discipline flags OFF
"""
import argparse
import atexit
import ctypes
import json
import os
import subprocess
import sys
import time

REPO = r"C:\Users\USER\companion-mcp"
VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
SWEDIR = os.path.join(REPO, ".fleet", "swe")
WORK = os.path.join(SWEDIR, "work")
PREDS = os.path.join(SWEDIR, "preds_solve")
LOG = os.path.join(SWEDIR, "solve_decoupled.log")
LOCK = os.path.join(SWEDIR, "solve_decoupled.lock")
DIFFGATE = os.path.join(REPO, "bench", "swe_diffgate.py")
SETUP = os.path.join(REPO, "bench", "swe_repos_setup_batch.py")

sys.path.insert(0, os.path.join(REPO, "bench"))
from swe_batch_setup import goal_text  # reuse the exact repo-agnostic goal text (+ scaffold lifts)


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def free_gb(path="C:\\"):
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), None, None, ctypes.byref(free))
    return free.value / (1024 ** 3)


def acquire_lock():
    if os.path.exists(LOCK):
        try:
            old = open(LOCK).read().strip()
        except Exception:
            old = ""
        if old:
            chk = subprocess.run(["tasklist", "/FI", "PID eq " + old], capture_output=True, text=True)
            if old in (chk.stdout or ""):
                log("another solve orchestrator (pid %s) running; exiting" % old)
                sys.exit(0)
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK) and os.remove(LOCK))


def captured(inst):
    return os.path.isfile(os.path.join(PREDS, inst + ".json"))


def repo_key(inst):
    return inst.rsplit("-", 1)[0]


def affinity_order(insts, spec):
    """Group same-repo instances contiguously so a chunk reuses one warm blobless clone."""
    return sorted(insts, key=lambda i: (spec.get(i, {}).get("repo", ""), i))


def chunks(seq, k):
    return [list(seq[i:i + k]) for i in range(0, len(seq), k)]


def stage(insts, spec_path, floor_gb):
    cmd = [VENVPY, SETUP, "--spec", spec_path, "--floor-gb", str(floor_gb), "--instances"] + insts
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    tail = "\n".join((r.stdout or "").strip().splitlines()[-3:])
    log("stage rc=%s: %s" % (r.returncode, tail.replace("\n", " | ")))
    return r.returncode == 0


def write_goals(insts, spec, goals_path):
    n = 0
    with open(goals_path, "w", encoding="utf-8", newline="\n") as f:
        for inst in insts:
            s = spec.get(inst)
            wt = os.path.join(WORK, "wt_" + inst)
            if not s or not os.path.isdir(wt):
                log("  SKIP (no spec/worktree): %s" % inst)
                continue
            lib = s["repo"].split("/")[-1]
            ps = s["problem_statement"]
            # Docker-FREE acceptance gate: accept DONE iff a non-empty diff exists.
            check_cmd = '"%s" "%s" "%s"' % (VENVPY, DIFFGATE, wt)
            goal = {"text": goal_text(lib, wt, ps), "cwd": wt,
                    "checks": [{"type": "shell", "cmd": check_cmd, "timeout": 120}]}
            f.write(json.dumps(goal, ensure_ascii=False) + "\n")
            n += 1
    return n


def run_fleet(goals_path, max_concurrent, max_turns, max_transient, effort):
    cmd = [VENVPY, "-m", "relay.fleet_runner", "--goals-file", goals_path,
           "--max-concurrent", str(max_concurrent), "--max-turns", str(max_turns),
           "--max-transient", str(max_transient), "--disk-floor-gb", "0",
           # effort=min => each worker is ONE tab (no refuter/research side-pages), so a cap of N
           # runs N tasks in parallel. auto/ultra reserve ~3 tabs/task (tab_weight), which on a
           # RAM-tight box collapses to 1 task at a time. The strong-scaffold discipline lives in
           # the GOAL TEXT (set via env below), so min still self-tests -- it just drops the
           # external review operators, giving a clean single-shot pass@1.
           "--effort", effort]
    log("fleet: %s" % " ".join(cmd[2:]))
    p = subprocess.Popen(cmd, cwd=REPO, env=dict(os.environ))
    p.wait()
    log("fleet exited rc=%s" % p.returncode)


def capture(insts):
    os.makedirs(PREDS, exist_ok=True)
    nonempty = 0
    for inst in insts:
        wt = os.path.join(WORK, "wt_" + inst)
        diff = ""
        if os.path.isdir(wt):
            try:
                diff = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True,
                                      timeout=60).stdout
            except Exception as e:
                log("  capture error %s: %s" % (inst, e))
        pred = [{"instance_id": inst, "model_patch": diff, "model_name_or_path": "companion"}]
        with open(os.path.join(PREDS, inst + ".json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(pred, f, ensure_ascii=False)
        if diff.strip():
            nonempty += 1
    return nonempty


def release(insts):
    """Drop each instance's worktree (keep the shared blobless clone + the captured diff).
    The clone that owns wt_<inst> is deterministically WORK/<repo_key>-main, because the
    instance id is '<repo-with-__>-<number>' and repo_dirname replaces '/' with '__'."""
    freed = 0
    for inst in insts:
        wt = os.path.join(WORK, "wt_" + inst)
        if not os.path.isdir(wt):
            continue
        clone = os.path.join(WORK, repo_key(inst) + "-main")
        try:
            if os.path.isdir(os.path.join(clone, ".git")):
                subprocess.run(["git", "-C", clone, "worktree", "remove", wt, "--force"],
                               capture_output=True, text=True)
            if os.path.isdir(wt):
                subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", wt], capture_output=True)
            if not os.path.isdir(wt):
                freed += 1
        except Exception:
            pass
    log("released %d worktree(s); C: free %.1f GB" % (freed, free_gb()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=os.path.join(SWEDIR, "all300_spec.json"))
    ap.add_argument("--targets-file", default=os.path.join(SWEDIR, "_all300.txt"))
    ap.add_argument("--chunk", type=int, default=25)
    ap.add_argument("--max-concurrent", type=int, default=2)
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--max-transient", type=int, default=8)
    ap.add_argument("--effort", default="min", choices=["min", "max", "ultra", "auto"],
                    help="fleet effort. min (default) = 1 tab/task -> real parallelism + clean "
                         "single-shot pass@1. auto/ultra reserve ~3 tabs/task (refuter+research).")
    ap.add_argument("--floor-gb", type=float, default=8.0,
                    help="abort staging a chunk if C: free < this (keeps the disk safe)")
    ap.add_argument("--keep-worktrees", action="store_true", help="do not release after capture")
    ap.add_argument("--baseline", action="store_true",
                    help="scaffold discipline flags OFF (SWE_STRONG_SELFTEST/MINIMALITY/FIX_RADIUS)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N instances (smoke test)")
    ap.add_argument("--preds-dir", default="",
                    help="override capture dir (so A/B arms keep separate prediction sets)")
    ap.add_argument("--tag", default="",
                    help="suffix for the log/lock files (lets two arms run without lock collision)")
    a = ap.parse_args()

    # Per-arm isolation: a separate preds dir means captured() does not skip the OTHER arm's
    # solves, and a tagged log/lock keeps the two arms' bookkeeping apart. The A/B variable itself
    # (SWE_MISS85_DISCIPLINE) is read from the environment by goal_text at write_goals() time and
    # propagated to the fleet subprocess via env=dict(os.environ) -- no code change needed for it.
    global PREDS, LOG, LOCK
    if a.preds_dir:
        PREDS = a.preds_dir if os.path.isabs(a.preds_dir) else os.path.join(SWEDIR, a.preds_dir)
    if a.tag:
        LOG = os.path.join(SWEDIR, "solve_decoupled_%s.log" % a.tag)
        LOCK = os.path.join(SWEDIR, "solve_decoupled_%s.lock" % a.tag)

    acquire_lock()
    os.makedirs(PREDS, exist_ok=True)

    # Fixed scaffold config (documented in the run). Strong = the believed-best, fully
    # domain-general discipline; baseline = flags off (a lower-bound control).
    if a.baseline:
        for k in ("SWE_STRONG_SELFTEST", "SWE_MINIMALITY", "SWE_FIX_RADIUS"):
            os.environ.pop(k, None)
        scaffold = "baseline (flags OFF)"
    else:
        os.environ["SWE_STRONG_SELFTEST"] = "1"
        os.environ["SWE_MINIMALITY"] = "1"
        os.environ["SWE_FIX_RADIUS"] = "1"
        scaffold = "strong (STRONG_SELFTEST+MINIMALITY+FIX_RADIUS)"

    spec = {s["instance_id"]: s for s in json.load(open(a.spec, encoding="utf-8"))}
    targets = [l.strip() for l in open(a.targets_file, encoding="utf-8") if l.strip()]
    ordered = affinity_order(targets, spec)
    remaining = [t for t in ordered if not captured(t)]
    if a.limit:
        remaining = remaining[:a.limit]

    log("=== decoupled solve start: %d/%d remaining (captured %d) | scaffold=%s | chunk=%d "
        "conc=%d turns=%d | C: free %.1f GB ===" %
        (len(remaining), len(targets), len(targets) - len([t for t in ordered if not captured(t)]),
         scaffold, a.chunk, a.max_concurrent, a.max_turns, free_gb()))

    for ci, ch in enumerate(chunks(remaining, a.chunk), 1):
        fg = free_gb()
        if fg < a.floor_gb:
            log("ABORT chunk %d: C: free %.1f GB < floor %.1f GB. Free disk and re-run (resumable)."
                % (ci, fg, a.floor_gb))
            break
        repos = sorted(set(repo_key(i).split("__")[0] for i in ch))
        log("--- chunk %d/%d: %d inst (repos: %s) | C: free %.1f GB ---"
            % (ci, len(chunks(remaining, a.chunk)), len(ch), ",".join(repos), fg))
        if not stage(ch, a.spec, a.floor_gb):
            log("  stage failed for chunk %d; skipping (will retry on resume)" % ci)
            continue
        goals_path = os.path.join(SWEDIR, "goals_solve_chunk.jsonl")
        ng = write_goals(ch, spec, goals_path)
        if ng == 0:
            log("  no goals written for chunk %d; skipping" % ci)
            continue
        run_fleet(goals_path, a.max_concurrent, a.max_turns, a.max_transient, a.effort)
        ne = capture(ch)
        log("  captured %d/%d (non-empty diffs: %d)" % (len(ch), len(ch), ne))
        if not a.keep_worktrees:
            release(ch)

    total = len([t for t in ordered if captured(t)])
    nonempty = 0
    for t in ordered:
        p = os.path.join(PREDS, t + ".json")
        if os.path.isfile(p):
            try:
                if json.load(open(p, encoding="utf-8"))[0]["model_patch"].strip():
                    nonempty += 1
            except Exception:
                pass
    log("=== solve done/paused: captured %d/%d (non-empty diffs: %d). Next: grade on kiyus "
        "(bench/swe_grade_batch.py) ===" % (total, len(targets), nonempty))


if __name__ == "__main__":
    main()
