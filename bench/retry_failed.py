"""Re-run the still-failing HumanEval problems from scratch: reset their solution.py to the
original stub and emit a goals file (retry3.jsonl). Usage:
  python -m bench.retry_failed HumanEval_8 HumanEval_97 HumanEval_98
"""
import json
import os
import sys


def main():
    targets = set(sys.argv[1:]) or {"HumanEval_8", "HumanEval_97", "HumanEval_98"}
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench = os.path.join(repo, ".fleet", "bench")
    goals = [json.loads(l) for l in open(os.path.join(bench, "goals.jsonl"), encoding="utf-8") if l.strip()]
    probs = {}
    for l in open(os.path.join(repo, "bench", "HumanEval.jsonl"), encoding="utf-8"):
        if l.strip():
            p = json.loads(l)
            probs[p["task_id"].replace("/", "_")] = p

    sel = []
    for g in goals:
        base = os.path.basename(g.get("cwd", "").replace("\\", "/").rstrip("/"))
        if base in targets:
            sel.append((base, g))

    print("selected:", sorted(b for b, _ in sel))
    for base, g in sel:
        p = probs[base]
        with open(os.path.join(bench, base, "solution.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write(p["prompt"])
    print("reset stubs:", len(sel))

    out = os.path.join(bench, "retry3.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for base, g in sel:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
