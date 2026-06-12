"""Split SWE-bench Lite 300 instances into dev_holdout / final_test / train_pool.

Purpose: detect scaffold over-fitting. Improvement decisions are based ONLY on
holdout pass-rate; detailed failure info from holdout must NOT feed back into
harness changes (see split_manifest.json policy notes).

Usage:
  # First run – generate splits
  python bench/swe_split.py

  # Re-run after files already exist (safety check; exits 0 if identical)
  python bench/swe_split.py

  # Overwrite existing files (e.g. after deliberate re-split)
  python bench/swe_split.py --force

Data source: WSL /root/swe/lite_local.json  (read via wsl cat, same as swe_select12.py pattern)

Split sizes  (from the 285 non-burned instances):
  dev_holdout  : 60  – harness-effect measurement; pass-rate only; failure details forbidden
  final_test   : 45  – sealed until final eval; do not open early
  train_pool   : 180 – free to use for harness development

Stratification: repository-proportional, seed=42, deterministic.
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

# ── paths ──────────────────────────────────────────────────────────────────
LITE_WSL   = "/root/swe/lite_local.json"
WSL_DISTRO = "MiasmaLab"

OUTDIR = os.path.join(
    os.path.dirname(__file__), "..", ".fleet", "swe"
)
OUTDIR = os.path.normpath(OUTDIR)

OUT_DEV    = os.path.join(OUTDIR, "holdout_dev.txt")
OUT_FINAL  = os.path.join(OUTDIR, "holdout_final.txt")
OUT_TRAIN  = os.path.join(OUTDIR, "train_pool.txt")
OUT_MANIF  = os.path.join(OUTDIR, "split_manifest.json")

# ── burned IDs ─────────────────────────────────────────────────────────────
BURNED_FIXED = {
    "astropy__astropy-12907",
    "astropy__astropy-14182",
    "astropy__astropy-14365",
}

BATCH12_TXT = os.path.join(OUTDIR, "batch_12.txt")

# ── split sizes ─────────────────────────────────────────────────────────────
SEED        = 42
N_DEV       = 60
N_FINAL     = 45
# train_pool = remainder (should be 180 given 285 - 60 - 45 = 180)

# ── helpers ─────────────────────────────────────────────────────────────────

def load_lite() -> list[dict]:
    """Read lite_local.json from WSL distro via subprocess, same pattern as swe_select12.py."""
    result = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "cat", LITE_WSL],
        capture_output=True,
    )
    if result.returncode != 0:
        sys.exit(
            f"ERROR: could not read {LITE_WSL} from WSL distro {WSL_DISTRO!r}:\n"
            + result.stderr.decode(errors="replace")
        )
    return json.loads(result.stdout)


def load_batch12_ids() -> set[str]:
    """Read batch_12.txt (Windows side, 12 instance IDs)."""
    if not os.path.isfile(BATCH12_TXT):
        sys.exit(f"ERROR: batch_12.txt not found at {BATCH12_TXT}")
    with open(BATCH12_TXT, encoding="utf-8") as f:
        ids = {line.strip() for line in f if line.strip()}
    return ids


def stratified_split(instances: list[dict], sizes: list[int], seed: int) -> list[list[str]]:
    """Repository-stratified deterministic split with exact split sizes.

    Algorithm:
      1. Group instance_ids by repo and shuffle each group with a per-repo seed.
      2. Interleave the groups into a single ranked list (repo-round-robin by repo size
         descending so large repos contribute proportionally across the full list).
      3. Assign positions to splits via systematic sampling: walk through the ranked
         list and assign each instance to the split whose running quota is most
         under-fulfilled (largest fractional deficit), breaking ties by split index.
         This guarantees the exact sizes in *sizes* while maximising stratification.

    Returns list of lists of instance_ids, one list per split (same order as *sizes*).
    """
    # ── shuffle within each repo ─────────────────────────────────────────
    by_repo: dict[str, list[str]] = defaultdict(list)
    for inst in instances:
        by_repo[inst["repo"]].append(inst["instance_id"])

    shuffled: dict[str, list[str]] = {}
    for repo in sorted(by_repo.keys()):
        ids = sorted(by_repo[repo])
        rng = random.Random(seed + int(hashlib.md5(repo.encode()).hexdigest(), 16))
        rng.shuffle(ids)
        shuffled[repo] = ids

    # ── interleave repos into a single ordered sequence ──────────────────
    # Round-robin by repo, largest-first so large repos spread evenly
    repos_by_size = sorted(shuffled.keys(), key=lambda r: -len(shuffled[r]))
    pointers = {r: 0 for r in repos_by_size}
    ranked: list[str] = []
    while True:
        added = False
        for repo in repos_by_size:
            p = pointers[repo]
            if p < len(shuffled[repo]):
                ranked.append(shuffled[repo][p])
                pointers[repo] += 1
                added = True
        if not added:
            break

    assert len(ranked) == len(instances)

    # ── assign to splits using largest-deficit rule ───────────────────────
    # deficit[i] = (ideal cumulative count) - (actual count so far) for split i
    total = sum(sizes)
    n_splits = len(sizes)
    splits: list[list[str]] = [[] for _ in range(n_splits)]
    counts = [0] * n_splits

    for k, iid in enumerate(ranked):
        # pick split with largest deficit: sizes[i]*(k+1)/total - counts[i]
        best_i = max(
            range(n_splits),
            key=lambda i: sizes[i] * (k + 1) / total - counts[i]
        )
        splits[best_i].append(iid)
        counts[best_i] += 1

    assert counts == sizes, f"Split size mismatch: got {counts}, expected {sizes}"
    return splits


def ids_hash(ids: list[str]) -> str:
    """Stable SHA-256 of sorted IDs, used for idempotency check."""
    digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
    return digest


def read_txt(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def write_txt(path: str, ids: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(ids)) + "\n")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing split files even if they differ.")
    args = parser.parse_args()

    # ── 1. Load data ──────────────────────────────────────────────────────
    print("Loading lite_local.json from WSL …")
    all_instances = load_lite()
    print(f"  Total records: {len(all_instances)}")
    assert len(all_instances) == 300, f"Expected 300 records, got {len(all_instances)}"

    # ── 2. Build burned set ───────────────────────────────────────────────
    batch12_ids = load_batch12_ids()
    print(f"  batch_12.txt: {len(batch12_ids)} IDs")
    burned = BURNED_FIXED | batch12_ids
    print(f"  Total burned: {len(burned)} IDs")
    assert len(burned) == 15, (
        f"Expected 15 burned IDs (3 fixed + 12 batch), got {len(burned)}. "
        "Check for overlap between fixed burns and batch_12.txt."
    )

    # ── 3. Filter to eligible pool ────────────────────────────────────────
    eligible = [inst for inst in all_instances if inst["instance_id"] not in burned]
    print(f"  Eligible after burn: {len(eligible)}")
    assert len(eligible) == 285, f"Expected 285 eligible, got {len(eligible)}"

    # ── 4. Stratified split ───────────────────────────────────────────────
    n_train = len(eligible) - N_DEV - N_FINAL
    assert n_train == 180, f"Expected 180 train, got {n_train}"

    splits = stratified_split(eligible, [N_DEV, N_FINAL, n_train], seed=SEED)
    dev_ids, final_ids, train_ids = splits

    # ── 5. Verify coverage ────────────────────────────────────────────────
    all_assigned = set(dev_ids) | set(final_ids) | set(train_ids) | burned
    all_source   = {inst["instance_id"] for inst in all_instances}

    assert len(dev_ids)   == N_DEV,    f"dev_holdout: expected {N_DEV},   got {len(dev_ids)}"
    assert len(final_ids) == N_FINAL,  f"final_test:  expected {N_FINAL}, got {len(final_ids)}"
    assert len(train_ids) == n_train,  f"train_pool:  expected {n_train}, got {len(train_ids)}"

    overlap_df = set(dev_ids) & set(final_ids)
    overlap_dt = set(dev_ids) & set(train_ids)
    overlap_ft = set(final_ids) & set(train_ids)
    assert not overlap_df, f"Overlap dev∩final: {overlap_df}"
    assert not overlap_dt, f"Overlap dev∩train: {overlap_dt}"
    assert not overlap_ft, f"Overlap final∩train: {overlap_ft}"

    assert all_assigned == all_source, (
        "Coverage mismatch!\n"
        f"  Missing from output: {all_source - all_assigned}\n"
        f"  Extra in output:     {all_assigned - all_source}"
    )
    print("  Coverage assertion PASSED - all 300 IDs accounted for.")

    # ── 6. Idempotency check ──────────────────────────────────────────────
    existing_files = [OUT_DEV, OUT_FINAL, OUT_TRAIN, OUT_MANIF]
    any_exist = any(os.path.isfile(p) for p in existing_files)

    if any_exist and not args.force:
        # Check if existing content matches what we would write
        mismatches = []
        for path, ids in [(OUT_DEV, dev_ids), (OUT_FINAL, final_ids), (OUT_TRAIN, train_ids)]:
            if os.path.isfile(path):
                existing = read_txt(path)
                if set(existing) != set(ids):
                    mismatches.append(path)
            else:
                mismatches.append(f"{path} (missing)")

        if mismatches:
            sys.exit(
                "ERROR: existing split files differ from what would be generated.\n"
                "Differing files:\n  " + "\n  ".join(mismatches) + "\n"
                "Use --force to overwrite (WARNING: this changes the holdout set)."
            )
        else:
            print("Existing files match - no-op (use --force to overwrite).")
            return

    # ── 7. Build repo-level breakdown ─────────────────────────────────────
    id_to_repo = {inst["instance_id"]: inst["repo"] for inst in all_instances}

    def repo_counts(ids: list[str]) -> dict[str, int]:
        return dict(Counter(id_to_repo[i] for i in ids))

    dev_by_repo   = repo_counts(dev_ids)
    final_by_repo = repo_counts(final_ids)
    train_by_repo = repo_counts(train_ids)
    burned_by_repo = repo_counts(list(burned))

    all_repos = sorted(
        set(dev_by_repo) | set(final_by_repo) | set(train_by_repo) | set(burned_by_repo)
    )

    # ── 8. Write output files ─────────────────────────────────────────────
    os.makedirs(OUTDIR, exist_ok=True)
    write_txt(OUT_DEV,   dev_ids)
    write_txt(OUT_FINAL, final_ids)
    write_txt(OUT_TRAIN, train_ids)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "total_source": len(all_instances),
        "burned": sorted(burned),
        "counts": {
            "burned":      len(burned),
            "eligible":    len(eligible),
            "dev_holdout": len(dev_ids),
            "final_test":  len(final_ids),
            "train_pool":  len(train_ids),
        },
        "repo_breakdown": {
            repo: {
                "dev_holdout": dev_by_repo.get(repo, 0),
                "final_test":  final_by_repo.get(repo, 0),
                "train_pool":  train_by_repo.get(repo, 0),
                "burned":      burned_by_repo.get(repo, 0),
                "total":       (dev_by_repo.get(repo, 0)
                                + final_by_repo.get(repo, 0)
                                + train_by_repo.get(repo, 0)
                                + burned_by_repo.get(repo, 0)),
            }
            for repo in all_repos
        },
        "hashes": {
            "dev_holdout": ids_hash(dev_ids),
            "final_test":  ids_hash(final_ids),
            "train_pool":  ids_hash(train_ids),
        },
        "policy": {
            "dev_holdout": (
                "Harness-effect measurement. Record pass-rate only. "
                "Failure details MUST NOT feed back into harness modifications."
            ),
            "final_test": (
                "Sealed until final evaluation. Do NOT open or inspect results "
                "during harness development."
            ),
            "train_pool": "Free to use for harness development and iteration.",
        },
    }

    with open(OUT_MANIF, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ── 9. Report ─────────────────────────────────────────────────────────
    print(f"\nWrote:")
    print(f"  {OUT_DEV}    ({len(dev_ids)} IDs)")
    print(f"  {OUT_FINAL}  ({len(final_ids)} IDs)")
    print(f"  {OUT_TRAIN}   ({len(train_ids)} IDs)")
    print(f"  {OUT_MANIF}")

    header = f"{'repo':40s}  {'dev':>4}  {'final':>5}  {'train':>5}  {'burned':>6}  {'total':>5}"
    print("\n" + header)
    print("-" * len(header))
    for repo in all_repos:
        b = manifest["repo_breakdown"][repo]
        print(f"{repo:40s}  {b['dev_holdout']:>4}  {b['final_test']:>5}  "
              f"{b['train_pool']:>5}  {b['burned']:>6}  {b['total']:>5}")
    print("-" * len(header))
    totals = manifest["counts"]
    print(f"{'TOTAL':40s}  {totals['dev_holdout']:>4}  {totals['final_test']:>5}  "
          f"{totals['train_pool']:>5}  {totals['burned']:>6}  "
          f"{totals['dev_holdout']+totals['final_test']+totals['train_pool']+totals['burned']:>5}")
    print("\nAll assertions PASSED.")


if __name__ == "__main__":
    main()
