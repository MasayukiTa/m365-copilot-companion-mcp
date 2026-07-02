"""Build per-miss analysis bundles for the failure-analysis workflow.

For each graded MISS (verdict='not'), assemble everything an analyst needs to diagnose WHY the
agent's patch failed -- the problem, the GOLD fix, the AGENT's diff, the official test breakdown
(which FAIL_TO_PASS are still failing = unfixed, which PASS_TO_PASS now fail = regression), the
test_output tail, and the agent's transcript tail -- into one .md per instance under
.fleet/swe/_miss/. The workflow fans out one analyst per bundle.

  python bench/swe_miss_bundle.py <grade_runid>
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swe_check_remote as R
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEDIR = os.path.join(REPO, ".fleet", "swe")
PREDS = os.path.join(SWEDIR, "preds_solve")
OUT = os.path.join(SWEDIR, "_miss")
PARQUET = os.path.join(SWEDIR, "SWE-bench_Lite_test.parquet")

runid = sys.argv[1] if len(sys.argv) > 1 else None
misses = [json.loads(l)["instance_id"] for l in open(os.path.join(SWEDIR, "grade_results.jsonl"), encoding="utf-8")
          if l.strip() and json.loads(l).get("verdict") == "not"]
print("misses to bundle:", len(misses))
os.makedirs(OUT, exist_ok=True)

# 1) pull test_output.txt + report.json for all misses from kiyus (copy WSL->windows, scp down)
pulled = {}
if runid:
    safe = {i: re.sub(r"[^A-Za-z0-9_.-]", "_", i) for i in misses}
    # copy each instance's two log files to the windows-shared staging on kiyus
    parts = []
    base = "/tmp/gb_%s/logs/run_evaluation/%s/companion" % (runid, runid)
    for i in misses:
        s = safe[i]
        parts.append("cp %s/%s/test_output.txt /mnt/c/wsl-setup/_miss/%s.to 2>/dev/null" % (base, i, s))
        parts.append("cp %s/%s/report.json /mnt/c/wsl-setup/_miss/%s.rep 2>/dev/null" % (base, i, s))
    cmd = "mkdir -p /mnt/c/wsl-setup/_miss; " + "; ".join(parts) + "; ls /mnt/c/wsl-setup/_miss | wc -l"
    print("kiyus copy:", R._wsl_token(cmd, timeout=120) or "(none)")
    # scp the whole staging dir down
    import subprocess
    local_logs = os.path.join(OUT, "_logs")
    os.makedirs(local_logs, exist_ok=True)
    subprocess.run(["scp", "-q", "-o", "ConnectTimeout=30", "-o", "BatchMode=yes", "-r",
                    "shuttle-scope:C:/wsl-setup/_miss/.", local_logs],
                   capture_output=True, text=True, timeout=180)
    for i in misses:
        s = safe[i]
        to = os.path.join(local_logs, s + ".to")
        rep = os.path.join(local_logs, s + ".rep")
        pulled[i] = (open(to, encoding="utf-8", errors="replace").read() if os.path.isfile(to) else "",
                     open(rep, encoding="utf-8", errors="replace").read() if os.path.isfile(rep) else "")

# 2) parquet rows
df = pd.read_parquet(PARQUET)
rowmap = {r["instance_id"]: r for _, r in df.iterrows()}


def _tx_tail(inst, n=10):
    fs = sorted(glob.glob(os.path.join(REPO, ".fleet", "transcripts", "*.jsonl")),
                key=os.path.getmtime, reverse=True)
    for f in fs:
        try:
            rows = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
        except Exception:
            continue
        if any(inst in json.dumps(e, ensure_ascii=False) for e in rows[:2]):
            out = []
            for e in rows[-n:]:
                role = e.get("role") or e.get("type") or "?"
                t = str(e.get("text") or e.get("content") or "")
                out.append("[%s] %s" % (role, t[:1500]))
            return "\n".join(out)
    return "(transcript not found)"


def _report_breakdown(rep_json):
    try:
        d = json.loads(rep_json)
        # report.json: {inst: {tests_status: {FAIL_TO_PASS:{success:[],failure:[]}, PASS_TO_PASS:{...}}}}
        inner = next(iter(d.values())) if d else {}
        ts = inner.get("tests_status", {}) or {}
        f2p = ts.get("FAIL_TO_PASS", {}) or {}
        p2p = ts.get("PASS_TO_PASS", {}) or {}
        return (f2p.get("failure", []) or [], p2p.get("failure", []) or [])
    except Exception:
        return ([], [])


for inst in misses:
    r = rowmap.get(inst)
    if r is None:
        continue
    diff = ""
    p = os.path.join(PREDS, inst + ".json")
    if os.path.isfile(p):
        try:
            diff = json.load(open(p, encoding="utf-8"))[0]["model_patch"]
        except Exception:
            pass
    to, rep = pulled.get(inst, ("", ""))
    unfixed, regressions = _report_breakdown(rep)
    f2p = r["FAIL_TO_PASS"]
    f2p = (f2p.tolist() if hasattr(f2p, "tolist") else f2p)
    body = []
    body.append("# MISS: %s  (repo %s)\n" % (inst, r["repo"]))
    body.append("## Problem statement\n%s\n" % str(r["problem_statement"])[:3500])
    body.append("## Official test verdict (from report.json)\n"
                "- UNFIXED FAIL_TO_PASS (the bug is still not fixed): %s\n"
                "- REGRESSIONS PASS_TO_PASS now failing (the patch BROKE these): %s\n"
                % (unfixed[:12] or "(none parsed)", regressions[:12] or "(none parsed)"))
    body.append("## Target tests (FAIL_TO_PASS the fix must make pass)\n%s\n" % (f2p[:8] if isinstance(f2p, list) else f2p))
    body.append("## GOLD patch (the correct fix)\n```diff\n%s\n```\n" % str(r["patch"])[:3000])
    body.append("## AGENT patch (what the companion produced -- FAILED)\n```diff\n%s\n```\n" % (diff[:3500] or "(empty)"))
    body.append("## test_output tail (actual failure)\n```\n%s\n```\n" % (to[-2500:] if to else "(no test output pulled)"))
    body.append("## Agent transcript tail (its reasoning / where it went wrong)\n%s\n" % _tx_tail(inst))
    open(os.path.join(OUT, re.sub(r"[^A-Za-z0-9_.-]", "_", inst) + ".md"), "w", encoding="utf-8").write("\n".join(body))

print("wrote %d bundles to %s" % (len(glob.glob(os.path.join(OUT, "*.md"))), OUT))
