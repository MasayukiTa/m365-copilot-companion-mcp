"""Select the first scale-up batch of SWE-bench Lite instances, optimizing for REPOSITORY
DIVERSITY over difficulty (the first batch exists to surface harness gaps across many repo
build environments, not to maximize score). One instance per repo, drawn from the diverse
non-django/non-sympy-heavy set plus a couple of the big two, choosing the smallest
problem_statement per repo for a fast clear signal. Deterministic.

  python bench/swe_select12.py [N]            # default 12, prints + writes batch file
Writes /root/swe/batch_<N>.txt (instance ids) when run under WSL swe-venv, else just prints.
"""
import json
import os
import sys

LITE = os.environ.get("SWE_LITE", "/root/swe/lite_local.json")
# Outputs must live on the Windows side (.fleet/swe/) because the rest of the pipeline
# (swe_run_until_done.py, swe_batch_setup.py) runs under Windows python. When this script runs
# under WSL, write through the /mnt/c mount.
OUTDIR = os.environ.get(
    "SWE_OUTDIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".fleet", "swe"))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12

# Diversity-first repo order. astropy excluded (already piloted: 12907/14182/14365).
# Each repo contributes ONE instance until we reach N; django/sympy come last and only
# to top up, so the batch stays maximally spread across distinct build environments.
REPO_ORDER = [
    "psf/requests",          # tiny pure-python lib, fast clone, simple env
    "pallets/flask",         # small web lib
    "pydata/xarray",         # numeric, different test stack
    "pylint-dev/pylint",     # static-analysis tooling
    "mwaskom/seaborn",       # plotting, matplotlib-adjacent
    "pytest-dev/pytest",     # the test runner itself
    "sphinx-doc/sphinx",     # docs toolchain
    "scikit-learn/scikit-learn",  # has C/Cython build -> stresses eval image
    "matplotlib/matplotlib", # heavy build, Agg backend
    "sympy/sympy",           # pure-python but huge, CAS
    "django/django",         # the dominant repo; include 1-2 for coverage
]


def n_f2p(r):
    v = r["FAIL_TO_PASS"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return -1
    return len(v) if isinstance(v, list) else -1


def main():
    d = json.load(open(LITE))
    by = {}
    for x in d:
        by.setdefault(x["repo"], []).append(x)

    picked = []
    used_repos = set()
    # round 1: one per repo in diversity order
    for repo in REPO_ORDER:
        if len(picked) >= N:
            break
        rows = by.get(repo, [])
        if not rows:
            continue
        # smallest problem_statement first (clearer, faster signal for batch 1)
        rows = sorted(rows, key=lambda r: (len(r["problem_statement"]), r["instance_id"]))
        picked.append(rows[0])
        used_repos.add(repo)
    # round 2: if still short of N, add a 2nd from django then sympy (largest pools)
    for repo in ["django/django", "sympy/sympy", "matplotlib/matplotlib",
                 "scikit-learn/scikit-learn"]:
        if len(picked) >= N:
            break
        rows = sorted(by.get(repo, []),
                      key=lambda r: (len(r["problem_statement"]), r["instance_id"]))
        for r in rows:
            if r["instance_id"] not in {p["instance_id"] for p in picked}:
                picked.append(r)
                break

    picked = picked[:N]
    print("selected %d:" % len(picked))
    for r in picked:
        print("  %-42s %-26s ps=%5d F2P=%d %s" % (
            r["instance_id"], r["repo"], len(r["problem_statement"]),
            n_f2p(r), r["base_commit"][:10]))

    # write a batch spec (instance_id list + a per-instance spec json the rest of the
    # pipeline reads, mirroring pilot_spec.json shape) to the Windows-side .fleet/swe dir
    outdir = OUTDIR
    os.makedirs(outdir, exist_ok=True)
    KEEP = ["instance_id", "repo", "base_commit", "version", "environment_setup_commit",
            "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS", "patch", "test_patch"]
    spec = [{k: r.get(k) for k in KEEP} for r in picked]
    try:
        with open(os.path.join(outdir, "batch_%d.txt" % N), "w") as f:
            f.write("\n".join(r["instance_id"] for r in picked) + "\n")
        with open(os.path.join(outdir, "batch_%d_spec.json" % N), "w") as f:
            json.dump(spec, f, ensure_ascii=False, default=str)
        print("wrote", os.path.join(outdir, "batch_%d.txt" % N),
              "and batch_%d_spec.json" % N)
    except OSError as e:
        print("(spec not written: %s)" % e)


if __name__ == "__main__":
    main()
