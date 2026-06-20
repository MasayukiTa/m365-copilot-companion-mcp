#!/usr/bin/env python3
"""Recompute the final SWE-bench Lite 300 score from official batch result files.

The final 2026-06-20 strong-scaffold run finished in two non-overlapping swebench batches:

    b0620191201  first 258 predictions
    b0620220832  remaining 42 predictions

Swebench `error` and `empty` ids are counted as not resolved for pass@1 here: the official
evaluator returned a completed report for them, so they are model/output misses, not host gaps.
"""
import argparse
import collections
import json
import math
import os


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
GRADE_DIR = os.path.join(SWEDIR, "_grade_batch")
DEFAULT_RUNS = ("b0620191201", "b0620220832")


def wilson(successes, total, z=1.96):
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    half = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total) / denom
    return center - half, center + half


def load_result(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: set(data.get(k, []) or []) for k in ("resolved", "unresolved", "error", "empty")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade-dir", default=GRADE_DIR)
    ap.add_argument("--targets-file", default=os.path.join(SWEDIR, "_all300.txt"))
    ap.add_argument("--runs", nargs="+", default=list(DEFAULT_RUNS))
    args = ap.parse_args()

    targets = [line.strip() for line in open(args.targets_file, encoding="utf-8") if line.strip()]
    target_set = set(targets)
    buckets = {k: set() for k in ("resolved", "unresolved", "error", "empty")}
    owner = {}
    for run_id in args.runs:
        path = os.path.join(args.grade_dir, run_id + ".batchresult.json")
        result = load_result(path)
        for kind, ids in result.items():
            for instance_id in ids:
                if instance_id in owner:
                    raise SystemExit("duplicate verdict for %s: %s and %s" %
                                     (instance_id, owner[instance_id], run_id))
                owner[instance_id] = run_id
                buckets[kind].add(instance_id)

    covered = set(owner)
    missing = sorted(target_set - covered)
    extra = sorted(covered - target_set)
    if missing or extra:
        raise SystemExit("coverage mismatch: missing=%d extra=%d\nmissing=%s\nextra=%s" %
                         (len(missing), len(extra), missing, extra))

    resolved = buckets["resolved"]
    total = len(covered)
    lo, hi = wilson(len(resolved), total)
    print("SWE-bench Lite 300 strong-scaffold score")
    print("runs: %s" % ", ".join(args.runs))
    print("resolved: %d/%d = %.1f%%" % (len(resolved), total, 100.0 * len(resolved) / total))
    print("Wilson 95%% CI: [%.1f%%, %.1f%%]" % (100.0 * lo, 100.0 * hi))
    print("counts: resolved=%d unresolved=%d error=%d empty=%d" %
          (len(buckets["resolved"]), len(buckets["unresolved"]),
           len(buckets["error"]), len(buckets["empty"])))
    if buckets["error"]:
        print("error ids: %s" % ", ".join(sorted(buckets["error"])))
    if buckets["empty"]:
        print("empty ids: %s" % ", ".join(sorted(buckets["empty"])))
    print()
    print("| repo | resolved | total | pass@1 |")
    print("|---|---:|---:|---:|")
    repo_total = collections.Counter(i.split("__", 1)[0] for i in covered)
    repo_ok = collections.Counter(i.split("__", 1)[0] for i in resolved)
    for repo in sorted(repo_total):
        n = repo_total[repo]
        x = repo_ok[repo]
        print("| %s | %d | %d | %.1f%% |" % (repo, x, n, 100.0 * x / n))


if __name__ == "__main__":
    main()
