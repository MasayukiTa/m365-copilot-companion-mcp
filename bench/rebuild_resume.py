"""Rebuild .fleet/bench/resume_goals.jsonl from the currently-UNSOLVED HumanEval problems,
and reset each unsolved solution.py back to its original stub so the re-run starts from scratch
(per the standing benchmark instruction: re-run unfinished from zero, never redo completed).

  python -m bench.rebuild_resume
"""
import json
import os
import subprocess
import sys


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench = os.path.join(repo, ".fleet", "bench")
    eval_py = os.path.join(repo, "bench", "eval_one.py")

    goals_all = [json.loads(l) for l in open(os.path.join(bench, "goals.jsonl"), encoding="utf-8") if l.strip()]

    def fname(g):
        cwd = g.get("cwd", "")
        return os.path.basename(cwd.replace("\\", "/").rstrip("/"))

    solved, unsolved = [], []
    for g in goals_all:
        safe = fname(g)
        folder = os.path.join(bench, safe)
        try:
            r = subprocess.run([sys.executable, eval_py, safe, folder],
                               capture_output=True, text=True, timeout=40)
            ok = r.returncode == 0
        except Exception:
            ok = False
        (solved if ok else unsolved).append((safe, g, folder))

    print("solved (%d): %s" % (len(solved), sorted(s for s, _, _ in solved)))
    print("unsolved (%d): %s" % (len(unsolved), sorted(s for s, _, _ in unsolved)))

    # reset each unsolved solution.py to its stub (the HumanEval prompt) for a from-scratch retry
    reset = 0
    for safe, g, folder in unsolved:
        stubp = os.path.join(folder, "_data", "prompt.txt")
        solp = os.path.join(folder, "solution.py")
        if os.path.exists(stubp):
            open(solp, "w", encoding="utf-8").write(open(stubp, encoding="utf-8").read())
            reset += 1
    print("reset stubs: %d / %d" % (reset, len(unsolved)))

    out = os.path.join(bench, "resume_goals.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for safe, g, folder in unsolved:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    print("wrote resume_goals.jsonl with %d goals" % len(unsolved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
