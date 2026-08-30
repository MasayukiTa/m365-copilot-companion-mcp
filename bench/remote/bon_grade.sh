#!/bin/bash
# Grade every best-of-N sample on the same instances.
#
# The samples differ: measured, all six patches differed between sample 1 and sample 2 while
# the three EFFORT arms had produced byte-identical outcomes on their own six. So the variance
# lives in the sampling, not in the effort -- which is the only condition under which
# best-of-N can add anything, and the reason these have to be graded separately rather than
# pooled.
exec > /mnt/c/swe-grade/bon/bon_grade.out 2>&1
set -u
REPO=/mnt/c/swe-grade/pro_repo
PY=/root/swe-venv/bin/python
BASE=/mnt/c/swe-grade/bon
RAW=$BASE/bon_raw.jsonl
say(){ echo "[$(date +%H:%M:%S)] $*"; }
freeG(){ df -BG / | awk 'NR==2{gsub(/G/,"",$4);print $4}'; }

say "START bon-grade free=$(freeG)G"
for t in $(seq 1 20); do docker info >/dev/null 2>&1 && break; sleep 3; done
docker info >/dev/null 2>&1 || { say "dockerd unreachable"; exit 1; }

say "pulling images once (kept)"
"$PY" -c "import json;[print('jefzda/sweap-images:'+json.loads(l)['dockerhub_tag']) for l in open('$RAW')]" \
  | xargs -P3 -I{} bash -c 'for t in 1 2 3; do timeout 900 docker pull "{}" >/dev/null 2>&1 && exit 0; sleep 15; done; echo "PULL FAILED {}"'
say "free after pulls=$(freeG)G"

cd "$REPO" || { say "cannot enter $REPO"; exit 1; }
for f in "$BASE"/grade_preds_*.json; do
  k=$(basename "$f" .json); k=${k#grade_preds_}
  OUT=$BASE/out_$k
  rm -rf "$OUT"; mkdir -p "$OUT"
  say "--- sample $k ---"
  timeout 20000 "$PY" swe_bench_pro_eval.py --use_local_docker --num_workers 2 \
    --raw_sample_path "$RAW" --patch_path "$f" \
    --scripts_dir run_scripts --dockerhub_username jefzda --output_dir "$OUT" 2>&1 | tail -2
  "$PY" - "$OUT" "$k" <<'PYEOF'
import json, os, sys
p = os.path.join(sys.argv[1], "eval_results.json")
if not os.path.exists(p):
    print("sample %s: NO verdict file" % sys.argv[2]); raise SystemExit(0)
m = json.load(open(p)); r = sum(1 for v in m.values() if v)
print("sample %s: RESOLVED %d/%d = %.1f%%" % (sys.argv[2], r, len(m), 100.0*r/max(1,len(m))))
PYEOF
done
say "DONE_BON_GRADE free=$(freeG)G"
