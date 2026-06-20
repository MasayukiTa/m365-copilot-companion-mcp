#!/usr/bin/env python3
"""Runs ON the eval host (inside the Ubuntu WSL). Grades a WHOLE predictions file with swebench's
native parallel evaluator (--max_workers), so each repo's env image is built ONCE and the
instances run concurrently inside one process -- no per-grade env-image build race. Writes a
compact result + a .done marker to the Windows-shared verdicts dir for the caller to scp back.

    python3 the eval host_batch_grade.py <predictions.json> <run_id> <max_workers> [dataset_name]
"""
import glob
import json
import os
import subprocess
import sys

preds_path = sys.argv[1]
run_id = sys.argv[2]
max_workers = sys.argv[3] if len(sys.argv) > 3 else "8"
dataset_name = sys.argv[4] if len(sys.argv) > 4 else "princeton-nlp/SWE-bench_Lite"

workdir = "/tmp/gb_" + run_id
os.makedirs(workdir, exist_ok=True)

cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
       "--dataset_name", dataset_name,
       "--predictions_path", preds_path,
       "--max_workers", str(max_workers),
       "--run_id", run_id,
       "--cache_level", "instance",
       "--timeout", "1800"]
r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
with open(os.path.join(workdir, "run.out"), "w") as f:
    f.write((r.stdout or "") + "\n---ERR---\n" + (r.stderr or ""))

out = {"resolved": [], "unresolved": [], "error": [], "empty": [], "report": ""}
# swebench writes <model_name_or_path>.<run_id>.json into the CWD (= workdir)
cands = glob.glob(os.path.join(workdir, "*." + run_id + ".json")) + glob.glob(os.path.join(workdir, "*.json"))
for rep in cands:
    try:
        d = json.load(open(rep))
        if isinstance(d, dict) and ("resolved_ids" in d or "unresolved_ids" in d):
            out["resolved"] = d.get("resolved_ids", []) or []
            out["unresolved"] = d.get("unresolved_ids", []) or []
            out["error"] = d.get("error_ids", []) or []
            out["empty"] = d.get("empty_patch_ids", []) or []
            out["report"] = os.path.basename(rep)
            break
    except Exception:
        pass

vdir = "/mnt/c/wsl-setup/verdicts"
os.makedirs(vdir, exist_ok=True)
dest = os.path.join(vdir, run_id + ".batchresult.json")
json.dump(out, open(dest, "w"))
with open(dest + ".done", "w") as f:
    f.write("DONE\n")
print("BATCH_DONE resolved=%d unresolved=%d error=%d empty=%d"
      % (len(out["resolved"]), len(out["unresolved"]), len(out["error"]), len(out["empty"])))
