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
import ctypes
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
DISTRO = "MiasmaLab"
# repo-affinity ENV-keep disk floor: if C: free drops below this, stop preserving ENV images
# (the SWE_KEEP_ENV optimization) and fall back to full prune so the disk never starves.
KEEP_ENV_FLOOR_GB = float(os.environ.get("SWE_KEEP_ENV_FLOOR_GB", "6"))


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


def free_gb(path="C:\\"):
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(path), None, None, ctypes.byref(free))
    return free.value / (1024 ** 3)


def repo_key(inst):
    """Derive the SWE-bench repo owner+name from an instance id so we can group by repo.

    instance ids are '<owner>__<name>-<number>', e.g. 'django__django-11999',
    'scikit-learn__scikit-learn-13584', 'sphinx-doc__sphinx-8595'. The repo is the part
    before the trailing '-<number>'. Used ONLY for scheduling (sort -> chunk) so same-repo
    instances run back-to-back and reuse a warm ENV image; it never affects grading.
    """
    return inst.rsplit("-", 1)[0]


def affinity_order(insts):
    """Stable-sort instances so same-repo ids are contiguous (repo-affinity scheduling).

    Stable on the original order within each repo, so behavior is deterministic and the only
    effect is clustering repos together -- the 2nd+ instance of a repo in a chunk reuses the
    warm `sweb.env.*` image instead of triggering a ~20min cold build.
    """
    return sorted(insts, key=repo_key)


def cleanup_repo_env(repo_k):
    """Remove that repo's cached SWE-bench ENV images (`sweb.env.*`) at a repo boundary.

    With SWE_KEEP_ENV=1, swe_check keeps the per-version ENV image alive across instances of a
    repo (so retries/next instance are warm). Once we move to the NEXT repo, the prior repo's
    ENV images are dead weight on C:, so the orchestrator sweeps them here. Best-effort; never
    raises. swebench ENV image naming: sweb.env.x86_64.<hash>:latest (not repo-tagged), so we
    cannot target a single repo precisely -- instead we sweep ALL dangling/unused env images,
    which is safe because the just-finished repo's env images are now unreferenced and a still-
    needed image (none, since we're between repos) would simply be rebuilt on demand.
    """
    if os.environ.get("SWE_KEEP_ENV") != "1":
        return  # nothing was kept; swe_check already pruned per-instance
    script = (
        "pgrep dockerd >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 8); "
        # remove env images then prune dangling layers; both no-ops if already gone
        "docker images 'sweb.env.*' -q | sort -u | xargs -r docker rmi -f 2>/dev/null || true; "
        "docker image prune -f 2>/dev/null || true")
    try:
        subprocess.run(["wsl.exe", "-d", DISTRO, "sh", "-c", script],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
        log("repo boundary: swept ENV images after repo '%s' (free C: %.1f GB)"
            % (repo_k, free_gb()))
    except Exception as exc:
        log("repo boundary ENV sweep error (non-fatal): %s" % exc)


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
    # repo-affinity ENV-keep disk guard: swe_check honors SWE_KEEP_ENV=1 to preserve the warm
    # `sweb.env.*` image between same-repo instances (avoids the ~20min cold rebuild). But if C:
    # is running low we must NOT keep extra images -- force the legacy full-prune path by
    # dropping SWE_KEEP_ENV for this fleet launch. The parent may have exported SWE_KEEP_ENV=1.
    if env.get("SWE_KEEP_ENV") == "1":
        fg = free_gb()
        if fg < KEEP_ENV_FLOOR_GB:
            env.pop("SWE_KEEP_ENV", None)
            log("disk guard: C: %.1f GB < floor %.1f GB -> disabling SWE_KEEP_ENV "
                "(full prune) for this chunk" % (fg, KEEP_ENV_FLOOR_GB))
        else:
            log("SWE_KEEP_ENV=1 active (C: %.1f GB >= floor %.1f GB): ENV image kept "
                "across same-repo instances" % (fg, KEEP_ENV_FLOOR_GB))
    p = subprocess.Popen(
        # ユーザー指示によりターン無制限 (2026-06-13): max-turns=0 = unlimited
        [VENVPY, "-m", "relay.fleet_runner", "--goals-file", GOALS,
         "--max-concurrent", str(ARGS.max_concurrent), "--max-turns", "0",
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
        # repo-affinity: order the remaining instances so same-repo ids are contiguous BEFORE
        # chunking. With ENV-keep on, the 2nd+ same-repo instance in a chunk reuses the warm
        # `sweb.env.*` image -> no ~20min cold rebuild. Pure scheduling; grading is unchanged.
        ordered = affinity_order(remaining)
        if ordered != remaining:
            log("  repo-affinity order: %s" % ordered)
        # process the instances chunk-by-chunk; status.json reflects only the most-recent fleet
        # launch, so collect each chunk's resolved set right after it ends.
        round_done = set()
        prev_repo = None
        for ci, chunk in enumerate(chunks(ordered, ARGS.chunk), 1):
            # repo boundary: if this chunk starts a NEW repo, sweep the previous repo's kept ENV
            # images (only does work when SWE_KEEP_ENV=1; otherwise swe_check already pruned).
            chunk_repo = repo_key(chunk[0]) if chunk else None
            if prev_repo is not None and chunk_repo != prev_repo:
                cleanup_repo_env(prev_repo)
            log("  chunk %d (repo=%s): %s" % (ci, chunk_repo, chunk))
            run_round(chunk)
            done = resolved_set()
            for i in [x for x in chunk if x in done]:
                log("RESOLVED: %s" % i)
                round_done.add(i)
            prev_repo = repo_key(chunk[-1]) if chunk else prev_repo
        # end of round: sweep the final repo's ENV images so they do not persist between rounds.
        if prev_repo is not None:
            cleanup_repo_env(prev_repo)
        remaining = [i for i in remaining if i not in round_done]
        if not remaining:
            log("=== ALL TARGETS RESOLVED in %d round(s) ===" % rnd)
            return
        log("still unresolved after round %d: %s" % (rnd, remaining))
    log("=== STOPPED after %d rounds, unresolved=%s ===" % (MAX_ROUNDS, remaining))


if __name__ == "__main__":
    main()
