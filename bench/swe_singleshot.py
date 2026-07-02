"""CLEAN single-shot SWE-bench pass@1 -- no confounders.

For each FRESH instance (not holdout, not burned):
  1. reset the worktree to base,
  2. drive the agent with the issue ONLY, via the single-run relay with NO acceptance check
     (run_relay checks=None) -> the agent produces ONE patch and never sees the official
     FAIL_TO_PASS/PASS_TO_PASS eval while solving (NO grader-iteration, NO regression feedback,
     NO test-name leakage),
  3. grade that single patch ONCE with the official swebench eval (swe_check).

This is standard SWE-bench pass@1 protocol. It deliberately avoids the confounders flagged
earlier: (a) the verify-retry loop that iterates against the grading tests, and (b) the
PASS_TO_PASS/FAIL_TO_PASS regression feedback (bench meta-info). Sequential = disk-safe.

  .venv\\Scripts\\python.exe bench/swe_singleshot.py [--spec .fleet/swe/clean_ss_spec.json] [--max-turns 28]
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "bench"))
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO, ".env"))
from swe_batch_setup import goal_text   # repo-agnostic SWE goal (issue only, no tests)

VENVPY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
AGENT = os.environ.get("MCP_FLEET_AGENT_URL") or os.environ.get("MCP_IMPL_AGENT_URL")


def reset_wt(wt):
    subprocess.run(["git", "-C", wt, "checkout", "--", "."], capture_output=True)
    subprocess.run(["git", "-C", wt, "clean", "-fd"], capture_output=True)


def solve(inst, wt, lib, ps, max_turns, research=False):
    """One agent attempt, NO acceptance check -> the agent's first DONE patch, ungraded.
    research=False (default) passes --no-research (research delegation OFF); research=True omits
    it so the agent may delegate RESEARCH: queries -- the A/B axis for the research on/off study."""
    goal = goal_text(lib, wt, ps)
    env = dict(os.environ)
    env["SWE_NO_REGRESSION_FEEDBACK"] = "1"   # belt-and-suspenders (no --check means no feedback anyway)
    args = [VENVPY, "-m", "relay.copilot_autopilot_relay",
            "--conversation-url", AGENT, "--goal", goal,
            "--run-id", "ss_" + inst.replace("__", "_"),
            "--max-turns", str(max_turns), "--per-turn-timeout", "1800"]
    if not research:
        args.append("--no-research")
    subprocess.run(args, cwd=REPO, env=env)


def grade(inst, wt):
    """Grade the single patch ONCE with the official swebench eval.
    Returns True=resolved (exit 0), False=genuine miss (exit 1), None=EVAL_ERROR (exit 2: the
    WSL/Docker host wedged and produced no report -- NOT a miss, must be excluded from pass@1)."""
    r = subprocess.run([VENVPY, os.path.join(REPO, "bench", "swe_check.py"), inst, wt],
                       cwd=REPO, capture_output=True, text=True, errors="replace")
    if r.returncode == 0:
        return True
    if r.returncode == 2:
        return None
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=os.path.join(SW, "clean_ss_spec.json"))
    ap.add_argument("--max-turns", type=int, default=28)
    ap.add_argument("--results", default=os.path.join(SW, "_clean_ss_results.txt"))
    ap.add_argument("--research", action="store_true",
                    help="enable research delegation (the research-ON arm of the A/B); default OFF")
    args = ap.parse_args()

    if not AGENT:
        print("no agent URL in .env"); return 1
    spec = json.load(open(args.spec, encoding="utf-8"))
    open(args.results, "a").close()
    done = {}
    for ln in open(args.results, encoding="utf-8"):
        ln = ln.strip()
        if " " in ln:
            v, i = ln.split(" ", 1)
            if v == "EVALERR":
                continue   # eval-host failure, not a final verdict -> re-attempt this instance
            done[i] = (v == "RESOLVED")

    res = dict(done)
    n = 0
    for s in spec:
        inst = s["instance_id"]; lib = s["repo"].split("/")[-1]
        n += 1
        wt = os.path.join(SW, "work", "wt_" + inst)
        if inst in done:
            print("[%d/%d] %s -> cached %s" % (n, len(spec), inst, "RESOLVED" if done[inst] else "not"))
            continue
        if not os.path.isdir(wt):
            print("[%d/%d] %s -> SKIP (no worktree -- stage first)" % (n, len(spec), inst))
            continue
        reset_wt(wt)
        solve(inst, wt, lib, s["problem_statement"], args.max_turns, research=args.research)
        ok = grade(inst, wt)
        # EVAL_ERROR (None): swe_check already recovered the host -> re-grade the SAME patch a
        # couple times before giving up; the worktree still holds the agent's edits.
        ge = 0
        while ok is None and ge < 2:
            ge += 1
            ok = grade(inst, wt)
        res[inst] = ok
        tag = "RESOLVED" if ok is True else ("EVALERR" if ok is None else "not")
        with open(args.results, "a", encoding="utf-8") as f:
            f.write(tag + " " + inst + "\n")
        npass = sum(1 for v in res.values() if v is True)
        ndone = sum(1 for v in res.values() if v is not None)   # EVAL_ERRORs excluded from denom
        print("[%d/%d] %s -> %s   (running pass@1 = %d/%d)" % (
            n, len(spec), inst, tag, npass, ndone))

    npass = sum(1 for v in res.values() if v is True)
    ndone = sum(1 for v in res.values() if v is not None)
    nerr = sum(1 for v in res.values() if v is None)
    print("\n=== CLEAN single-shot pass@1: %d/%d ===" % (npass, ndone))
    if nerr:
        print("(%d instance(s) excluded as EVAL_ERROR -- host wedge, re-run when healthy)" % nerr)
    print("(no grader-iteration, no regression feedback, fresh non-holdout instances)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
