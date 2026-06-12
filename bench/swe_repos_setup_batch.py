"""Generalized repo + worktree preparation for a SWE-bench batch (replaces the astropy-only
swe_repos_setup.sh). Reads a batch spec (list of {instance_id, repo, base_commit}) and, for
each DISTINCT repo, makes ONE blobless clone (--filter=blob:none, shared object store) then a
detached worktree per instance at its base_commit.

Design choices for the disk-constrained laptop (C: ~16 GB free):
  * blobless clone (`--filter=blob:none`): full history graph, blobs fetched on demand. An
    astropy worktree measured ~36 MB; the shared clone ~58 MB. So a repo with k instances is
    roughly (clone + 36*k) MB -- a 12-instance / 11-repo batch is well under ~2 GB.
  * SEQUENTIAL clone (network-friendly, and lets the disk guard abort before the next repo).
  * DISK GUARD: abort before any clone if free space on C: would risk dropping under the floor
    (default 10 GB). Never let a clone push the disk past the limit.

  python bench/swe_repos_setup_batch.py [--spec PATH] [--floor-gb 10] [--instances id ...]
Default spec: .fleet/swe/batch_12_spec.json (written by swe_select12.py). Idempotent: existing
clones are reused; existing worktrees are removed and re-added clean.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"
WORK = os.path.join(REPO, ".fleet", "swe", "work")


def free_gb(path):
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(path), None, None, ctypes.byref(free))
    return free.value / (1024 ** 3)


def run(cmd, **kw):
    print("  $", " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    return subprocess.run(cmd, **kw)


def repo_dirname(repo):
    # "django/django" -> "django__django-main" ; "scikit-learn/scikit-learn" -> "scikit-learn__..."
    return repo.replace("/", "__") + "-main"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=os.path.join(REPO, ".fleet", "swe", "batch_12_spec.json"))
    ap.add_argument("--floor-gb", type=float, default=10.0)
    ap.add_argument("--instances", nargs="*", default=None,
                    help="optional subset of instance_ids to prepare")
    args = ap.parse_args()

    spec = json.load(open(args.spec, encoding="utf-8"))
    if args.instances:
        want = set(args.instances)
        spec = [s for s in spec if s["instance_id"] in want]
    if not spec:
        print("no instances to prepare"); return 1

    os.makedirs(WORK, exist_ok=True)

    # group instances by repo so we clone each repo once
    by_repo = {}
    for s in spec:
        by_repo.setdefault(s["repo"], []).append(s)

    print("=== batch repo setup: %d instance(s) across %d repo(s) ===" %
          (len(spec), len(by_repo)))
    print("free on C: %.1f GB (floor %.1f GB)" % (free_gb("C:\\"), args.floor_gb))

    prepared, failed = [], []
    for repo, insts in by_repo.items():
        clone_dir = os.path.join(WORK, repo_dirname(repo))
        url = "https://github.com/%s.git" % repo
        if not os.path.isdir(os.path.join(clone_dir, ".git")):
            fg = free_gb("C:\\")
            if fg < args.floor_gb:
                print("ABORT: free %.1f GB < floor %.1f GB before cloning %s" %
                      (fg, args.floor_gb, repo))
                failed += [s["instance_id"] for s in insts]
                break
            print("cloning %s (blobless) -> %s" % (repo, os.path.basename(clone_dir)))
            r = run(["git", "clone", "--filter=blob:none", "--no-checkout", url, clone_dir],
                    capture_output=True, text=True)
            if r.returncode != 0:
                print("  CLONE FAILED:", (r.stderr or "")[-400:])
                failed += [s["instance_id"] for s in insts]
                continue
        else:
            print("reuse existing clone %s" % os.path.basename(clone_dir))

        for s in insts:
            inst, base = s["instance_id"], s["base_commit"]
            wt = os.path.join(WORK, "wt_" + inst)
            run(["git", "-C", clone_dir, "worktree", "remove", "-f", wt],
                capture_output=True)
            if os.path.isdir(wt):
                run(["cmd", "/c", "rmdir", "/s", "/q", wt], capture_output=True)
            r = run(["git", "-C", clone_dir, "worktree", "add", "-f", "--detach", wt, base],
                    capture_output=True, text=True)
            if r.returncode != 0:
                print("  WORKTREE FAILED %s: %s" % (inst, (r.stderr or "")[-300:]))
                failed.append(inst)
            else:
                prepared.append(inst)
                print("  worktree %s @ %s" % (inst, base[:10]))

    print("--- done: %d prepared, %d failed ---" % (len(prepared), len(failed)))
    if failed:
        print("FAILED:", " ".join(failed))
    print("free on C: now %.1f GB" % free_gb("C:\\"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
