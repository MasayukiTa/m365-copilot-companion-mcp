"""Select batch_30: 30 instances from train_pool for the second scale-up batch.

Selection strategy:
- Source: .fleet/swe/train_pool.txt ONLY (no holdout or burned instances)
- Exclude: any instance already in batch_12.txt (or any --exclude file)
- Stratified by repository: allocate slots proportionally to each repo's
  train_pool count, then fill remaining slots with the largest pools.
- Within each repo: sort by problem_statement length ASC (shorter = clearer
  signal) then instance_id (deterministic tiebreak). Pick from the front.
- Seed: 42 (deterministic; used only for final shuffle before writing)
- Output: .fleet/swe/batch_30.txt + .fleet/swe/batch_30_spec.json

Usage:
  python bench/swe_select30.py [--n 30] [--pool .fleet/swe/train_pool.txt]
                                [--exclude .fleet/swe/batch_12.txt]
                                [--parquet .fleet/swe/SWE-bench_Lite_test.parquet]
"""
import argparse
import json
import math
import os
import random
import sys

REPO = r"C:\Users\USER\companion-mcp"
OUTDIR = os.path.join(REPO, ".fleet", "swe")
PARQUET = os.path.join(OUTDIR, "SWE-bench_Lite_test.parquet")
POOL_FILE = os.path.join(OUTDIR, "train_pool.txt")
EXCLUDE_FILE = os.path.join(OUTDIR, "batch_12.txt")
KEEP = ["instance_id", "repo", "base_commit", "version", "environment_setup_commit",
        "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS", "patch", "test_patch"]
SEED = 42


def load_ids(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def load_parquet(path):
    try:
        import pyarrow.parquet as pq
        tbl = pq.read_table(path)
        rows = tbl.to_pydict()
        n = len(rows["instance_id"])
        records = []
        for i in range(n):
            r = {k: rows[k][i] for k in rows}
            # normalise FAIL_TO_PASS / PASS_TO_PASS: may be stored as list or JSON string
            for fld in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                v = r.get(fld)
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                    r[fld] = v
            records.append(r)
        return records
    except ImportError:
        pass
    # fallback: try lite_local.json (WSL path exposed via env)
    lite = os.environ.get("SWE_LITE")
    if lite and os.path.isfile(lite):
        return json.load(open(lite, encoding="utf-8"))
    raise SystemExit("Cannot load instance data: pyarrow not installed and SWE_LITE not set")


def n_f2p(r):
    v = r.get("FAIL_TO_PASS", [])
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return -1
    return len(v) if isinstance(v, list) else -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--pool", default=POOL_FILE)
    ap.add_argument("--exclude", nargs="*", default=[EXCLUDE_FILE])
    ap.add_argument("--parquet", default=PARQUET)
    ap.add_argument("--outdir", default=OUTDIR)
    args = ap.parse_args()

    N = args.n

    # ----- load pool -----
    pool_ids = set(load_ids(args.pool))
    print("train_pool: %d instances" % len(pool_ids))

    # ----- load excludes -----
    exclude_ids = set()
    for ef in (args.exclude or []):
        if ef and os.path.isfile(ef):
            ids = load_ids(ef)
            exclude_ids.update(ids)
            print("exclude from %s: %d ids" % (ef, len(ids)))
    eligible_ids = pool_ids - exclude_ids
    print("eligible after exclude: %d" % len(eligible_ids))

    # ----- load parquet / instance data -----
    all_records = load_parquet(args.parquet)
    by_id = {r["instance_id"]: r for r in all_records}

    # restrict to eligible
    eligible = {iid: by_id[iid] for iid in eligible_ids if iid in by_id}
    if len(eligible) < N:
        raise SystemExit("Only %d eligible instances, cannot pick %d" % (len(eligible), N))

    # ----- stratified selection -----
    # group eligible by repo
    by_repo = {}
    for iid, r in eligible.items():
        by_repo.setdefault(r["repo"], []).append(r)

    # sort within each repo: shorter problem_statement first, then instance_id
    for repo in by_repo:
        by_repo[repo].sort(key=lambda r: (len(r["problem_statement"]), r["instance_id"]))

    repos = sorted(by_repo.keys())
    total_eligible = sum(len(v) for v in by_repo.values())

    # proportional allocation: floor(N * repo_count / total), at least 1 if available
    alloc = {}
    allocated = 0
    for repo in repos:
        cnt = len(by_repo[repo])
        a = math.floor(N * cnt / total_eligible)
        alloc[repo] = a
        allocated += a

    # distribute remainder to repos with largest fractional parts (tiebreak: most eligible)
    remainders = N - allocated
    fracs = []
    for repo in repos:
        cnt = len(by_repo[repo])
        exact = N * cnt / total_eligible
        frac = exact - math.floor(exact)
        fracs.append((frac, cnt, repo))
    fracs.sort(key=lambda x: (-x[0], -x[1]))
    for i in range(remainders):
        alloc[fracs[i][2]] += 1

    # clamp: can't pick more than available
    for repo in repos:
        alloc[repo] = min(alloc[repo], len(by_repo[repo]))

    # if total < N after clamping, fill from repos with most surplus (deterministic)
    total_picked = sum(alloc.values())
    if total_picked < N:
        surplus = [(len(by_repo[r]) - alloc[r], r) for r in repos]
        surplus.sort(key=lambda x: (-x[0], x[1]))
        for surplus_cnt, repo in surplus:
            if total_picked >= N:
                break
            if surplus_cnt > 0:
                need = min(N - total_picked, surplus_cnt)
                alloc[repo] += need
                total_picked += need

    # pick instances
    picked = []
    for repo in repos:
        k = alloc[repo]
        if k > 0:
            picked.extend(by_repo[repo][:k])

    # deterministic shuffle (stable selection, but randomised order for the fleet scheduler)
    rng = random.Random(SEED)
    rng.shuffle(picked)
    picked = picked[:N]

    # ----- report -----
    print("\nSelected %d instances:" % len(picked))
    repo_counts = {}
    for r in picked:
        repo_counts.setdefault(r["repo"], 0)
        repo_counts[r["repo"]] += 1
    for repo in sorted(repo_counts):
        print("  %-40s %2d  (pool=%d)" % (repo, repo_counts[repo], len(by_repo[repo])))
    print()
    for r in sorted(picked, key=lambda x: x["instance_id"]):
        print("  %-42s ps=%5d F2P=%d %s" % (
            r["instance_id"], len(r["problem_statement"]),
            n_f2p(r), r["base_commit"][:10]))

    # ----- write outputs -----
    os.makedirs(args.outdir, exist_ok=True)
    txt_path = os.path.join(args.outdir, "batch_30.txt")
    spec_path = os.path.join(args.outdir, "batch_30_spec.json")

    with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(r["instance_id"] for r in picked) + "\n")
    print("wrote", txt_path)

    spec = [{k: r.get(k) for k in KEEP} for r in picked]
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, default=str)
    print("wrote", spec_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
