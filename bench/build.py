"""build.py -- materialize a HumanEval subset into per-problem folders + a goals file for
the relay fleet. The agent sees ONLY solution.py (the function signature + docstring); the
canonical test lives in bench/_data (hidden) and is run by eval_one.py as the gate check.

  python bench/build.py [--stride 8] [--limit 20]
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=8)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default=".fleet/bench")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    vendpy = os.path.join(repo, ".venv", "Scripts", "python.exe")
    data_dir = os.path.join(here, "_data")
    os.makedirs(data_dir, exist_ok=True)
    out_root = os.path.join(repo, args.out)
    os.makedirs(out_root, exist_ok=True)

    probs = [json.loads(l) for l in open(os.path.join(here, "HumanEval.jsonl"),
                                         encoding="utf-8") if l.strip()]
    subset = probs[::args.stride][:args.limit]

    goals = []
    for p in subset:
        safe = p["task_id"].replace("/", "_")
        folder = os.path.join(out_root, safe)
        os.makedirs(folder, exist_ok=True)
        # the stub the agent completes (signature + docstring only)
        with open(os.path.join(folder, "solution.py"), "w", encoding="utf-8", newline="\n") as f:
            f.write(p["prompt"])
        # hidden test data (NOT in the agent's folder)
        with open(os.path.join(data_dir, safe + ".json"), "w", encoding="utf-8") as f:
            json.dump({"test": p["test"], "entry_point": p["entry_point"]}, f, ensure_ascii=False)
        eval_py = os.path.join(here, "eval_one.py")
        check_cmd = '"%s" "%s" %s "%s"' % (vendpy, eval_py, safe, folder)
        goal = {
            "text": ("solution.py には関数の signature と docstring があります。"
                     "docstring の仕様どおりにこの関数を完成させてください"
                     "（replace_in_file / write_file で solution.py を編集）。"
                     "run_python で自分でも入力例を試して正しく動くことを確認すること。"
                     "テストファイルは見えませんが、仕様を満たせば自動検証が通ります。"
                     "完成したら DONE。フォルダ: %s" % folder),
            "cwd": folder,
            "checks": [{"type": "shell", "cmd": check_cmd, "timeout": 40}],
        }
        goals.append(goal)

    goals_path = os.path.join(out_root, "goals.jsonl")
    with open(goals_path, "w", encoding="utf-8", newline="\n") as f:
        for g in goals:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    print("built %d problems (stride=%d) -> %s" % (len(goals), args.stride, out_root))
    print("goals file: %s" % goals_path)
    print("task ids:", ", ".join(p["task_id"] for p in subset))


if __name__ == "__main__":
    main()
