#!/bin/bash
# Grade the three effort arms on the SAME six instances.
#
# The arms produced the same outcome (all DONE) and nearly the same turn count, so the
# self-report cannot separate them -- which is the whole reason this has to be graded. Cost
# without a correctness number is not a comparison, it is a price list.
exec > /mnt/c/swe-grade/ab/ab_grade.out 2>&1
set -u
REPO=/mnt/c/swe-grade/pro_repo
PY=/root/swe-venv/bin/python
BASE=/mnt/c/swe-grade/ab
RAW=$BASE/ab_raw.jsonl
say(){ echo "[$(date +%H:%M:%S)] $*"; }
freeG(){ df -BG / | awk 'NR==2{gsub(/G/,"",$4);print $4}'; }

say "START ab-grade free=$(freeG)G"
for t in $(seq 1 20); do docker info >/dev/null 2>&1 && break; sleep 3; done
docker info >/dev/null 2>&1 || { say "dockerd unreachable"; exit 1; }

say "pulling the six images once (kept, not pruned)"
"$PY" -c "import json;[print('jefzda/sweap-images:'+json.loads(l)['dockerhub_tag']) for l in open('$RAW')]" \
  | xargs -P3 -I{} bash -c 'for t in 1 2 3; do timeout 900 docker pull "{}" >/dev/null 2>&1 && exit 0; sleep 15; done; echo "PULL FAILED {}"'
say "free after pulls=$(freeG)G"

cd "$REPO" || { say "cannot enter $REPO"; exit 1; }
for arm in min auto ultra; do
  OUT=$BASE/out_$arm
  rm -rf "$OUT"; mkdir -p "$OUT"
  say "--- arm $arm ---"
  timeout 20000 "$PY" swe_bench_pro_eval.py --use_local_docker --num_workers 2 \
    --raw_sample_path "$RAW" --patch_path "$BASE/grade_preds_$arm.json" \
    --scripts_dir run_scripts --dockerhub_username jefzda --output_dir "$OUT" 2>&1 | tail -2
  "$PY" - "$OUT" "$arm" <<'PYEOF'
import json, os, sys
p = os.path.join(sys.argv[1], "eval_results.json")
if not os.path.exists(p):
    print("arm %s: NO verdict file" % sys.argv[2]); raise SystemExit(0)
m = json.load(open(p)); r = sum(1 for v in m.values() if v)
print("arm %s: RESOLVED %d/%d = %.1f%%" % (sys.argv[2], r, len(m), 100.0*r/max(1,len(m))))
PYEOF
done
say "DONE_AB_GRADE free=$(freeG)G"
