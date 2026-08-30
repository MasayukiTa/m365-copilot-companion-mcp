#!/bin/bash
# Grade the UI-driven Pro slice on the eval host.
#
# NO PRUNE JANITOR, and that is the point of this rewrite. The script this replaces ran a
# background loop that called `docker system prune -af` whenever free space fell below
# 305 GB -- and the box now has 281 GB free, so it would have pruned continuously from the
# first second, turning the whole run into pull-delete-pull. That is exactly the churn the
# owner forbade on this drive after destroying a personal SSD the same way: "書き込み→削除
# →書き込みの反復は厳禁 ... 一度書いたら基本消さない".
#
# So: measure first, pull once, keep. Measured 2026-08-30 with `docker manifest inspect`,
# without pulling anything: 40 instances, 52 GB compressed, mean 1.3 GB. Uncompressed that
# is roughly 105-130 GB against 190 GB free on the eval host's docker filesystem,
# which on this particular box is a WSL virtual disk capped at 230 GB so it cannot eat the
# Windows volume below ~80 GB. Nothing here depends on that; it is one host's arrangement.
#
# If it does not fit, this STOPS and says so. It does not free space by deleting what it
# just downloaded.
exec > /mnt/c/swe-grade/ui_20260829/grade.out 2>&1
set -u
REPO=/mnt/c/swe-grade/pro_repo
PY=/root/swe-venv/bin/python
BASE=/mnt/c/swe-grade/ui_20260829
RAW=$BASE/ui_raw_40.jsonl
PREDS=$BASE/ui_preds.json
OUT=$BASE/out
FLOOR_GB=${SWE_FLOOR_GB:-25}

mkdir -p "$OUT"
freeG(){ df -BG / | awk 'NR==2{gsub(/G/,"",$4);print $4}'; }
say(){ echo "[$(date +%H:%M:%S)] $*"; }

say "START ui40  free=$(freeG)G on the docker filesystem  floor=${FLOOR_GB}G"

for t in $(seq 1 20); do docker info >/dev/null 2>&1 && break; sleep 3; done
docker info >/dev/null 2>&1 || { say "dockerd unreachable"; exit 1; }
say "dockerd ok"

# ONLY INSTANCES THAT CARRY A PATCH ARE GRADED. A row with an empty patch is not a failed
# fix, it is an instance nobody worked, and scoring it as a failure is how a run that never
# happened becomes an accuracy number.
"$PY" - "$PREDS" "$RAW" "$OUT" <<'PYEOF'
import json, sys
preds = json.load(open(sys.argv[1]))
have = {p["instance_id"] for p in preds if (p.get("patch") or "").strip()}
raw = [json.loads(l) for l in open(sys.argv[2]) if l.strip()]
keep = [r for r in raw if r["instance_id"] in have]
with open(sys.argv[3] + "/raw_graded.jsonl", "w") as f:
    f.writelines(json.dumps(r) + "\n" for r in keep)
with open(sys.argv[3] + "/preds_graded.json", "w") as f:
    json.dump([p for p in preds if p["instance_id"] in have], f)
print("grading %d of %d instances (%d carry no patch and are NOT scored)"
      % (len(keep), len(raw), len(raw) - len(keep)))
PYEOF

# PULL EVERYTHING FIRST, then evaluate. Pulling as you go is what makes the working set
# unbounded; pulling first makes the footprint knowable before any grading starts, and the
# floor check below can refuse while refusing is still cheap.
say "pulling images (once, and they are kept)"
"$PY" -c "import json;[print('jefzda/sweap-images:'+json.loads(l)['dockerhub_tag']) for l in open('$OUT/raw_graded.jsonl')]" \
  | xargs -P3 -I{} bash -c 'for t in 1 2 3; do timeout 900 docker pull "{}" >/dev/null 2>&1 && exit 0; sleep 15; done; echo "PULL FAILED {}"'
say "images on disk: $(docker images -q | sort -u | wc -l)  free=$(freeG)G"

if [ "$(freeG)" -lt "$FLOOR_GB" ]; then
  say "STOPPING: free space $(freeG)G is below the ${FLOOR_GB}G floor."
  say "Not deleting anything to make room -- that is the churn this script exists to avoid."
  exit 2
fi

# cd HERE, AND NOT ONLY IN A COMMENT. The eval entry point is a path relative to the harness
# repository, and this script is launched from a scheduled task whose working directory is
# C:\Windows\system32. It failed with
#     can't open file '/mnt/c/Windows/system32/swe_bench_pro_eval.py'
# after thirteen minutes of pulling forty-four images -- the expensive half had already
# succeeded and the cheap half could not find its own program. The rewrite that dropped the
# cd inherited everything else from the script it replaced.
cd "$REPO" || { say "cannot enter $REPO"; exit 1; }
say "evaluating (cwd=$(pwd))"
timeout 30000 "$PY" swe_bench_pro_eval.py --use_local_docker --num_workers 2 \
  --raw_sample_path "$OUT/raw_graded.jsonl" --patch_path "$OUT/preds_graded.json" \
  --scripts_dir run_scripts --dockerhub_username jefzda --output_dir "$OUT" 2>&1 | tail -3

"$PY" - "$OUT" <<'PYEOF'
import json, sys, os
p = os.path.join(sys.argv[1], "eval_results.json")
if not os.path.exists(p):
    print("NO eval_results.json -- the evaluation did not produce a verdict file")
    raise SystemExit(1)
m = json.load(open(p))
r = sum(1 for v in m.values() if v)
print("RESOLVED %d/%d graded = %.1f%%" % (r, len(m), 100.0 * r / max(1, len(m))))
print("end-to-end over the whole 40-instance slice: %d/40 = %.1f%%" % (r, 100.0 * r / 40.0))
PYEOF

say "DONE_UI40 free=$(freeG)G"
