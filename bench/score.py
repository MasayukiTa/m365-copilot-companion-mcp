"""score.py -- ground-truth tally of a HumanEval benchmark run.

Independent of the worker outcome labels (which can be wrong under harness flakiness, e.g.
a mislabeled STUCK), this re-runs the hidden canonical test on every produced solution.py
and reports pass@1 = solved / total. Each problem is evaluated in its own subprocess with a
timeout so a runaway solution can't hang the tally.

  python bench/score.py [--out .fleet/bench]
"""
import argparse
import json
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".fleet/bench")
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument("--status", default=None, help="run's status.json for label comparison")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    eval_py = os.path.join(here, "eval_one.py")
    out_root = os.path.join(repo, args.out)

    folders = sorted(d for d in os.listdir(out_root)
                     if os.path.isdir(os.path.join(out_root, d)) and d.startswith("HumanEval_"))
    passed, results = 0, []
    for safe in folders:
        folder = os.path.join(out_root, safe)
        try:
            r = subprocess.run([sys.executable, eval_py, safe, folder],
                               capture_output=True, text=True, timeout=args.timeout)
            ok = r.returncode == 0
            tag = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "NO_OUTPUT"
        except subprocess.TimeoutExpired:
            ok, tag = False, "TIMEOUT"
        except Exception as e:
            ok, tag = False, "ERR:" + type(e).__name__
        passed += 1 if ok else 0
        results.append((safe, ok, tag))

    total = len(folders)
    truth = dict((safe, ok) for safe, ok, _ in results)

    # optional: compare ground truth to the worker OUTCOME labels, to quantify the harness
    # false-negative rate (a solution that passes but whose worker was mislabeled STUCK).
    labels, mislabeled = {}, []
    status_path = args.status or os.path.join(repo, ".fleet", "status.json")
    try:
        d = json.load(open(status_path, encoding="utf-8"))
        for w in (d.get("workers") or []):
            g = w.get("goal") or ""
            if "HumanEval_" in g:
                key = "HumanEval_" + g.split("HumanEval_", 1)[1].split("/")[0].split("\\")[0].split()[0]
                labels[key] = w.get("outcome")
    except Exception:
        pass
    for safe in folders:
        if truth.get(safe) and labels.get(safe) not in ("DONE", None):
            mislabeled.append((safe, labels.get(safe)))

    print("=== HumanEval subset (ground-truth re-evaluation) ===")
    for safe, ok, tag in results:
        lab = labels.get(safe)
        print("  [%s] %-16s %s%s" % ("PASS" if ok else "FAIL", safe,
                                     "" if ok else tag,
                                     ("  (worker=%s)" % lab) if lab and (ok != (lab == "DONE")) else ""))
    rate = (100.0 * passed / total) if total else 0.0
    print("\npass@1 (ground truth) = %d / %d = %.1f%%" % (passed, total, rate))
    if labels:
        done = sum(1 for k in folders if labels.get(k) == "DONE")
        print("worker DONE labels    = %d / %d" % (done, total))
        if mislabeled:
            print("harness false-negatives (solved but worker!=DONE): %d  -> %s"
                  % (len(mislabeled), ", ".join("%s/%s" % m for m in mislabeled)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
