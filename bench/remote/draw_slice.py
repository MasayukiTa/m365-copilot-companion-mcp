"""Draw a fresh SWE-bench-Pro slice ON THE EVAL HOST, where the dataset already lives.

RUN IT THERE, NOT HERE. Both existing slices are used up -- pro_slice50_full is burned and
pro_slice40_fresh is 40/40 graded -- so any new number needs a new draw, and the dataset is
2 GB that this laptop does not have room for and does not need. The host already has it
cached, so the laptop receives only the slice: 486 KB for 40 instances.

    scp draw_slice.py exclude_ids.json  ->  C:/swe-grade/
    ssh <host> wsl -d Ubuntu -u root /root/swe-venv/bin/python /mnt/c/swe-grade/draw_slice.py
    scp C:/swe-grade/pro_slice_fresh_new.json  ->  .fleet/swe/

exclude_ids.json is burned_ids() | graded_ids() | the ids of every existing slice, written by
the caller. 641 of 811 instances were still unused at the first draw.

Nothing is downloaded to the operator's laptop: the full set is already cached here
(helper_code/sweap_eval_full_v2.jsonl and the HF cache), so the laptop receives only the
resulting slice -- about 36 KB per instance.

Deterministic: sorted, then a fixed seed. A slice drawn differently each time cannot be
reproduced, and a benchmark you cannot re-draw is one you cannot check.
"""
import hashlib
import io
import json
import os
import random

#: The HF cache holds the COMPLETE records. helper_code/sweap_eval_full_v2.jsonl is the eval
#: helper and lacks six fields the harness needs (dockerhub_tag, fail_to_pass, interface,
#: pass_to_pass, repo_language, requirements) -- the first draw stopped on exactly that rather
#: than emitting a slice that would fail halfway through a run.
ARROW = ("/root/.cache/huggingface/datasets/ScaleAI___swe-bench_pro/default/0.0.0/"
         "7ab5114912baf22bb098818e604c02fe7ad2c11f/swe-bench_pro-test.arrow")
EXCL = "/mnt/c/swe-grade/exclude_ids.json"
OUT = "/mnt/c/swe-grade/pro_slice_fresh_new.json"
WANT = int(os.environ.get("SLICE_N", "40"))

#: The schema the local harness expects, taken from the slice it already runs.
REQUIRED = ["base_commit", "before_repo_set_cmd", "dockerhub_tag", "fail_to_pass",
            "instance_id", "interface", "pass_to_pass", "problem_statement", "repo",
            "repo_language", "requirements", "selected_test_files_to_run"]

excluded = set(json.load(io.open(EXCL, encoding="utf-8")))

import pyarrow.ipc as ipc

rows = []
keys_seen = set()
with open(ARROW, "rb") as fh:
    reader = ipc.open_stream(fh)
    table = reader.read_all()
keys_seen = set(table.column_names)
for r in table.to_pylist():
    if r.get("instance_id") and r["instance_id"] not in excluded:
        rows.append(r)

print("dataset rows read      : usable %d" % len(rows))
print("excluded (already used): %d" % len(excluded))
missing = [k for k in REQUIRED if k not in keys_seen]
print("required keys missing  : %s" % (missing or "none"))
if missing:
    # Fail loudly rather than emit a slice the harness will choke on halfway through a run.
    raise SystemExit("dataset lacks required fields: %s" % missing)

# Stratify by repo so one project cannot dominate the draw, then a fixed seed inside each.
by_repo = {}
for r in rows:
    by_repo.setdefault(r.get("repo") or "?", []).append(r)
for k in by_repo:
    by_repo[k].sort(key=lambda r: r["instance_id"])

rnd = random.Random(20260902)
picked, repos = [], sorted(by_repo)
while len(picked) < WANT and any(by_repo[k] for k in repos):
    for k in repos:
        if len(picked) >= WANT:
            break
        if by_repo[k]:
            picked.append(by_repo[k].pop(rnd.randrange(len(by_repo[k]))))

picked = [{k: r.get(k) for k in REQUIRED} for r in picked]
picked.sort(key=lambda r: r["instance_id"])

io.open(OUT, "w", encoding="utf-8").write(json.dumps(picked, ensure_ascii=False))
digest = hashlib.sha256(json.dumps([r["instance_id"] for r in picked]).encode()).hexdigest()[:16]
counts = {}
for r in picked:
    counts[r["repo"]] = counts.get(r["repo"], 0) + 1
print("drawn                  : %d" % len(picked))
print("by repo                : %s" % json.dumps(counts, sort_keys=True))
print("slice id digest        : %s" % digest)
print("written                : %s (%.1f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))
