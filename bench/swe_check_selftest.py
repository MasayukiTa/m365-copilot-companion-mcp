"""Validate swe_check.py end-to-end: apply the GOLD patch to a worktree, run the gate, expect
RESOLVED (exit 0), then revert the worktree clean. Confirms the diff->eval->parse wiring before
the agent is involved. Also warms the astropy image cache for the agent runs.
"""
import json
import os
import subprocess
import sys

REPO = r"C:\Users\USER\companion-mcp"
inst = sys.argv[1] if len(sys.argv) > 1 else "astropy__astropy-12907"
wt = os.path.join(REPO, ".fleet", "swe", "work", "wt_" + inst)

spec = {t["instance_id"]: t for t in
        json.load(open(os.path.join(REPO, ".fleet", "swe", "pilot_spec.json"), encoding="utf-8"))}
patch = spec[inst]["patch"]
pf = os.path.join(REPO, ".fleet", "swe", "_gold_" + inst + ".patch")
with open(pf, "w", encoding="utf-8", newline="\n") as f:
    f.write(patch)

# clean first, then apply gold
subprocess.run(["git", "-C", wt, "checkout", "--", "."])
subprocess.run(["git", "-C", wt, "clean", "-fd"], capture_output=True)
ap = subprocess.run(["git", "-C", wt, "apply", pf], capture_output=True, text=True)
if ap.returncode != 0:
    print("GOLD APPLY FAILED:", ap.stderr[:300])
    sys.exit(2)
print("gold patch applied; running swe_check (this runs the Docker eval, ~5 min)...")

rc = subprocess.run([sys.executable, os.path.join(REPO, "bench", "swe_check.py"), inst, wt]).returncode
print("=== swe_check rc = %d (0 = RESOLVED expected) ===" % rc)

# revert worktree so the agent starts from the clean base_commit
subprocess.run(["git", "-C", wt, "checkout", "--", "."])
subprocess.run(["git", "-C", wt, "clean", "-fd"], capture_output=True)
print("worktree reverted to clean base_commit")
print("SELFTEST_RESULT=%s" % ("PASS" if rc == 0 else "FAIL"))
