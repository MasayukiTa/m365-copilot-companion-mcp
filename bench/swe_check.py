"""SWE-bench acceptance gate for the fleet. Given an instance_id, take the agent's edits in
its worktree (git diff), run the OFFICIAL swebench evaluation in WSL2 Docker, and exit 0 only
if the hidden tests pass (instance resolved). On failure, print actionable feedback (re-injected
to the agent by the relay).

  python bench/swe_check.py <instance_id> [<worktree_path>]
Exit 0 = resolved (DONE accepted). Exit 1 = not resolved / no patch (keep working).
"""
import glob
import json
import os
import re
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"
DISTRO = "MiasmaLab"


def wsl(script, timeout=1000, capture=False):
    # decode as utf-8/replace: WSL test logs contain bytes the Windows cp932 default can't decode,
    # which would otherwise crash the subprocess reader thread and yield empty output.
    return subprocess.run(["wsl.exe", "-d", DISTRO, "sh", "-c", script],
                          capture_output=capture, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def main():
    inst = sys.argv[1]
    wt = sys.argv[2] if len(sys.argv) > 2 else os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)

    # 1. the agent's patch = git diff in its worktree
    g = subprocess.run(["git", "-C", wt, "diff"], capture_output=True, text=True)
    diff = g.stdout
    if not diff.strip():
        print("NO_PATCH_YET: you have not edited any files in the repository at %s. "
              "Read the relevant source, fix the bug, then save your edits." % wt)
        return 1

    # 2. write predictions (Windows path; WSL reads it via /mnt/c)
    preds_dir = os.path.join(REPO, ".fleet", "swe", "preds")
    os.makedirs(preds_dir, exist_ok=True)
    predpath = os.path.join(preds_dir, inst + ".json")
    with open(predpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump([{"instance_id": inst, "model_patch": diff,
                    "model_name_or_path": "companion"}], f)
    predwsl = "/mnt/c/Users/USER/companion-mcp/.fleet/swe/preds/" + inst + ".json"

    # 3. official eval in WSL Docker (cache_level env keeps the image for fast retries)
    run_id = "agent_" + inst.replace("__", "_")
    script = (
        "pgrep dockerd >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 8); "
        "export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt; cd /root/swe; "
        "/root/swe-venv/bin/python -m swebench.harness.run_evaluation "
        "--dataset_name /root/swe/lite_local.json --predictions_path " + predwsl + " "
        "--instance_ids " + inst + " --run_id " + run_id + " --max_workers 1 --cache_level env"
    )
    try:
        wsl(script, timeout=1200)
    except subprocess.TimeoutExpired:
        print("EVAL_TIMEOUT: the evaluation took too long. Likely the image pull is slow; "
              "your patch may still be fine. Keep it and it will be re-checked.")
        return 1

    # 4. read the report (swebench writes companion.<run_id>.json into /root/swe)
    r = wsl("cat /root/swe/companion." + run_id + ".json 2>/dev/null", timeout=60, capture=True)
    out = r.stdout if r.stdout and r.stdout.strip() else ""
    if not out:
        r2 = wsl("ls /root/swe/*" + run_id + "*.json 2>/dev/null | head -1 | xargs cat 2>/dev/null",
                 timeout=60, capture=True)
        out = r2.stdout or ""
    resolved = False
    try:
        d = json.loads(out)
        resolved = inst in (d.get("resolved_ids") or [])
    except Exception:
        resolved = False

    if resolved:
        print("RESOLVED: the hidden tests pass for %s." % inst)
        return 0

    # 5. NOT resolved -> surface the REAL test failure to the agent (failing test names +
    #    the assertion/traceback tail) so it can locate the exact missed spot. This is the
    #    feedback an Anthropic-grade harness gives; a generic "tests fail" leaves the agent blind.
    feedback = _failure_feedback(run_id, inst)
    print("NOT_RESOLVED: the hidden tests still fail with your current patch for %s. "
          "Find the real root cause in the SOURCE (do not edit tests) and fix it.\n%s"
          % (inst, feedback))
    return 1


def _failure_feedback(run_id, inst):
    """Extract failing test names + the last assertion/traceback from the swebench test log."""
    logp = "/root/swe/logs/run_evaluation/" + run_id + "/companion/" + inst + "/test_output.txt"
    r = wsl("cat " + logp + " 2>/dev/null", timeout=60, capture=True)
    log = r.stdout or ""
    if not log.strip():
        return "(no test log captured; re-read the failing test and trace each code path it exercises.)"
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)  # strip ANSI color codes pytest emits
    lines = log.splitlines()
    failed = [ln.strip() for ln in lines if ln.lstrip().startswith("FAILED ")]
    # last assertion: lines that pytest marks with a leading 'E ' (the error), keep the tail few
    err = [ln for ln in lines if ln.lstrip().startswith("E ")]
    err_tail = err[-6:] if err else []
    # the source line pytest points at: a '<path>:<n>: <Error>' just after the E-block
    ptr = [ln.strip() for ln in lines
           if (".py:" in ln and ln.rstrip().endswith(("Error", "Exception"))
               or ": ValueError" in ln or ": TypeError" in ln or ": KeyError" in ln)]
    parts = ["--- ACTUAL TEST FAILURE (use this to find the exact spot) ---"]
    if failed:
        parts.append("Failing tests:")
        parts.extend("  " + f for f in failed[:6])
    if err_tail:
        parts.append("Error:")
        parts.extend("  " + e.strip() for e in err_tail)
    if ptr:
        parts.append("Raised at:")
        parts.extend("  " + p for p in ptr[-4:])
    parts.append("Hint: the same bug pattern often appears in MORE than one place in the file; "
                 "search for every occurrence, not just the first.")
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
