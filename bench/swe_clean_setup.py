"""Build a FRESH, balanced clean-measurement slice (no holdout, no burned) + its spec, straight
from the SWE-bench Lite dataset. Output: .fleet/swe/clean_ss_spec.json (for staging + goals) and
.fleet/swe/clean_ss.txt (the instance list). Deterministic (sorted, no randomness).

  .venv\\Scripts\\python.exe bench/swe_clean_setup.py [--per "sympy:3,django:3,..."]
"""
import argparse
import json
import os
import subprocess
from collections import defaultdict

REPO = r"C:\Users\USER\companion-mcp"
SW = os.path.join(REPO, ".fleet", "swe")
HOLD = set(l.strip() for l in open(os.path.join(SW, "holdout_dev.txt"), encoding="utf-8") if l.strip())
BURNED = {"psf__requests-2148", "psf__requests-2317", "sphinx-doc__sphinx-7738",
          "matplotlib__matplotlib-18869"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", default="sympy:3,django:3,scikit-learn:2,sphinx:2,pytest:2")
    ap.add_argument("--out-spec", default=os.path.join(SW, "clean_ss_spec.json"))
    ap.add_argument("--out-list", default=os.path.join(SW, "clean_ss.txt"))
    args = ap.parse_args()

    plan = []
    for tok in args.per.split(","):
        repo, k = tok.split(":")
        plan.append((repo.strip(), int(k)))

    out = subprocess.run(["wsl.exe", "-d", "MiasmaLab", "--", "cat", "/root/swe/lite_local.json"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    data = json.loads(out.stdout)

    by = defaultdict(list)
    for d in data:
        iid = d["instance_id"]
        if iid in HOLD or iid in BURNED:
            continue
        by[d["repo"].split("/")[-1]].append(d)

    spec = []
    for repo, k in plan:
        cands = sorted(by.get(repo, []), key=lambda d: d["instance_id"])[:k]
        for d in cands:
            spec.append({"instance_id": d["instance_id"], "repo": d["repo"],
                         "base_commit": d["base_commit"], "problem_statement": d["problem_statement"]})

    with open(args.out_spec, "w", encoding="utf-8", newline="\n") as f:
        json.dump(spec, f, ensure_ascii=False)
    with open(args.out_list, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(s["instance_id"] for s in spec) + "\n")

    print("clean fresh slice: %d instances (all NOT in holdout, NOT burned)" % len(spec))
    for s in spec:
        print("  ", s["instance_id"])
    print("spec ->", args.out_spec)


if __name__ == "__main__":
    main()
