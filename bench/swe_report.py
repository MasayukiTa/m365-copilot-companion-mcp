"""Print the summary counts from a swebench run_evaluation report JSON. Run with WSL venv:
  /root/swe-venv/bin/python swe_report.py /root/swe/<report>.json
If no path given, picks the newest *pilot*_gold*.json under /root/swe.
"""
import glob
import json
import os
import sys

path = sys.argv[1] if len(sys.argv) > 1 else None
if not path:
    cands = sorted(glob.glob("/root/swe/*gold*.json"), key=os.path.getmtime)
    path = cands[-1] if cands else None
if not path:
    print("no report found")
    raise SystemExit(1)

d = json.load(open(path))
print("report:", path)
for k in ("total_instances", "submitted_instances", "completed_instances",
          "resolved_instances", "unresolved_instances", "empty_patch_instances",
          "error_instances"):
    if k in d:
        print("  %-22s %s" % (k, d[k]))
print("  resolved_ids:", d.get("resolved_ids"))
