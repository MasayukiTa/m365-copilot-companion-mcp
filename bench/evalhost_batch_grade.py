#!/usr/bin/env python3
"""Runs ON the eval host (inside the Ubuntu WSL). Grades a WHOLE predictions file with swebench's
native parallel evaluator (--max_workers), so each repo's env image is built ONCE and the
instances run concurrently inside one process -- no per-grade env-image build race. Writes a
compact result + a .done marker to the Windows-shared verdicts dir for the caller to scp back.

    python3 evalhost_batch_grade.py <predictions.json> <run_id> <max_workers> [dataset_name]
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
run_out_text = (r.stdout or "") + "\n---ERR---\n" + (r.stderr or "")
with open(os.path.join(workdir, "run.out"), "w") as f:
    f.write(run_out_text)

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

# WHY THIS FIELD EXISTS: `workdir` (/tmp/gb_<run_id>) is not durable enough to debug against --
# measured 2026-09-09, it was gone by the time a caller came back to read run.out even a few
# minutes later (something on this host cleans /tmp), while a run that produced no "resolved"/
# "unresolved"/"error" entries gives the caller nothing to act on. swebench's own stderr is
# where the real reason (missing image, bad predictions_path, docker daemon down, etc.) lives,
# so a bounded tail of it travels inside the one file this script's own caller already scp's
# back reliably (the batchresult.json, in the persistent Windows-shared verdicts dir) instead of
# requiring a second, racy remote fetch.
if not out["resolved"] and not out["unresolved"] and not out["error"] and not out["empty"]:
    out["stderr_tail"] = (r.stderr or "")[-4000:]
    out["returncode"] = r.returncode

vdir = "/mnt/c/wsl-setup/verdicts"
os.makedirs(vdir, exist_ok=True)
dest = os.path.join(vdir, run_id + ".batchresult.json")
json.dump(out, open(dest, "w"))
# THE SAME DURABILITY PROBLEM, for the raw output: copy run.out next to the batchresult.json
# BEFORE the workdir can be cleaned up, so a caller that wants the full (not 4000-char-truncated)
# log can scp %s/verdicts/<run_id>.run.out reliably too.
try:
    with open(os.path.join(vdir, run_id + ".run.out"), "w") as f:
        f.write(run_out_text)
except Exception:
    pass
with open(dest + ".done", "w") as f:
    f.write("DONE\n")
print("BATCH_DONE resolved=%d unresolved=%d error=%d empty=%d"
      % (len(out["resolved"]), len(out["unresolved"]), len(out["error"]), len(out["empty"])))
