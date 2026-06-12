"""Convert the local SWE-bench Lite parquet to the JSON list swebench's harness accepts as
a local --dataset_name (so evaluation never touches the corp-blocked HF). Run with WSL venv.
"""
import json
import sys

import pyarrow.parquet as pq

PARQUET = sys.argv[1] if len(sys.argv) > 1 else \
    "/mnt/c/Users/USER/companion-mcp/.fleet/swe/SWE-bench_Lite_test.parquet"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/root/swe/lite_local.json"

df = pq.read_table(PARQUET).to_pandas()
recs = []
for _, r in df.iterrows():
    d = {}
    for k in df.columns:
        v = r[k]
        if hasattr(v, "tolist"):
            v = v.tolist()
        d[k] = v
    recs.append(d)

with open(OUT, "w") as f:
    json.dump(recs, f, ensure_ascii=False, default=str)
print("wrote %s with %d instances" % (OUT, len(recs)))
