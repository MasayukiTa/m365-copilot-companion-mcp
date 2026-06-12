"""Load SWE-bench Lite from the LOCAL parquet (fetched via the prod box; no HF access).
Verify it and select a deterministic pilot subset. Run with the WSL swe-venv python.
"""
import json
import os
import sys

import pyarrow.parquet as pq

PARQUET = sys.argv[1] if len(sys.argv) > 1 else \
    "/mnt/c/Users/USER/companion-mcp/.fleet/swe/SWE-bench_Lite_test.parquet"

df = pq.read_table(PARQUET).to_pandas()
print("rows:", len(df))
print("cols:", list(df.columns))

df = df.sort_values("instance_id").reset_index(drop=True)
N = int(os.environ.get("SWE_N", "5"))
pick = df.head(N)

os.makedirs("/root/swe", exist_ok=True)
KEEP = ["instance_id", "repo", "base_commit", "version",
        "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS", "patch", "test_patch"]
spec = []
for _, r in pick.iterrows():
    row = {}
    for k in KEEP:
        v = r[k] if k in r else None
        if hasattr(v, "tolist"):
            v = v.tolist()
        row[k] = v
    spec.append(row)

with open("/root/swe/pilot_spec.json", "w") as f:
    json.dump(spec, f, ensure_ascii=False, default=str)
# also a full-set spec for later scaling
with open("/root/swe/all_ids.txt", "w") as f:
    f.write("\n".join(df["instance_id"].tolist()) + "\n")

print("pilot N=%d:" % N)
for s in spec:
    f2p = s["FAIL_TO_PASS"]
    n_f2p = len(f2p) if isinstance(f2p, list) else (len(json.loads(f2p)) if isinstance(f2p, str) else "?")
    print("  %-28s %-22s %s  F2P=%s" % (s["instance_id"], s["repo"], s["base_commit"][:10], n_f2p))
