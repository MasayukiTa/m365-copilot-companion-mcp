"""Run the oracle suite through the fleet, with and without the deliverable contract, and grade.

PAIRED: every task is run under BOTH arms, so a task that is simply hard does not land on one
arm and look like an effect. The arms differ by exactly one appended block, which a test pins.

THE ONLY SCORE IS THE ORACLE'S. Nothing here reads the worker's claim about whether it
checked, split, terminated, or finished. `outcome == "DONE"` is recorded for diagnosis and is
NOT the result: it is the worker reporting that it stopped, and reporting it as accuracy is the
error this whole suite exists to correct.

Usage:
  python -m bench.run_oracle_ab --reps 3 --state-root .fleet/oracle
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench.oracle_suite_repo import tasks, truths        # noqa: E402


def _final_answer(state_dir):
    tx = sorted(glob.glob(os.path.join(state_dir, "transcripts", "*.jsonl")))
    if not tx:
        return ""
    rows = [json.loads(l) for l in io.open(tx[-1], encoding="utf-8") if l.strip()]
    a = [r for r in rows if r.get("role") == "assistant"]
    return a[-1].get("text", "") if a else ""


def _outcome(state_dir):
    """The worker's own terminal state. Diagnostic only -- never the result."""
    p = os.path.join(state_dir, "status.json")
    try:
        st = json.load(io.open(p, encoding="utf-8"))
        ws = st.get("workers") or []
        return (ws[0].get("outcome") or "") if ws else ""
    except (OSError, ValueError, IndexError, AttributeError):
        return ""


def run_one(task, with_contract, state_dir, timeout_s=900):
    os.makedirs(state_dir, exist_ok=True)
    gf = os.path.join(state_dir, "goal.jsonl")
    with io.open(gf, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"goal": task.full_goal(with_contract)}, ensure_ascii=False) + "\n")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.time()
    with io.open(os.path.join(state_dir, "run.log"), "w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", "relay.fleet_runner",
                        "--goals-file", gf, "--state-dir", state_dir,
                        "--effort", "min", "--max-concurrent", "1"],
                       cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                       timeout=timeout_s)
    answer = _final_answer(state_dir)
    graded = task.grade(answer)
    return {
        "task": task.tid, "arm": "contract" if with_contract else "control",
        "passed": graded["passed"], "detail": graded["detail"],
        "outcome": _outcome(state_dir),        # diagnostic, not the score
        "seconds": round(time.time() - started, 1),
        "answer_len": len(answer),
        "started": started, "ended": time.time(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--state-root", default=os.path.join(REPO, ".fleet", "oracle"))
    a = ap.parse_args(argv)

    ts = tasks()
    print("truths: %s" % truths())
    out_path = os.path.join(a.state_root, "results.jsonl")
    os.makedirs(a.state_root, exist_ok=True)
    for rep in range(1, a.reps + 1):
        for task in ts:
            for with_contract in (False, True):     # paired, control first
                arm = "contract" if with_contract else "control"
                sd = os.path.join(a.state_root, "%s_%s_r%d" % (task.tid, arm, rep))
                try:
                    rec = run_one(task, with_contract, sd)
                except subprocess.TimeoutExpired:
                    rec = {"task": task.tid, "arm": arm, "passed": False,
                           "detail": "run timed out", "outcome": "", "seconds": None,
                           "answer_len": 0, "started": None, "ended": time.time()}
                rec["rep"] = rep
                with io.open(out_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print("%s r%d %-9s %-18s %s  %ss  (%s)"
                      % (time.strftime("%H:%M:%S"), rep, arm, task.tid,
                         "PASS" if rec["passed"] else "fail", rec["seconds"], rec["detail"]))
    print("ORACLE_AB_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
