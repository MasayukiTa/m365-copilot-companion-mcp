#!/bin/bash
# Grade the UI-driven 40-instance Pro slice. Runs on the eval host, not on the box that
# produced the patches: that box has single-digit GB free and this needs hundreds.
exec > /mnt/c/swe-grade/ui_20260829/grade.out 2>&1
set -u
REPO=/mnt/c/swe-grade/pro_repo; PY=/root/swe-venv/bin/python
RAW=/mnt/c/swe-grade/ui_20260829/ui_raw_40.jsonl
PREDS=/mnt/c/swe-grade/ui_20260829/ui_preds.json
OUT=/mnt/c/swe-grade/ui_20260829/out
rm -rf "$OUT"; mkdir -p "$OUT"; rm -f /tmp/ui_grade_done; cd "$REPO"
freeG(){ df -BG /mnt/c | awk 'NR==2{gsub(/G/,"",$4);print $4}'; }
echo "[$(date +%H:%M:%S)] START ui40 free=$(freeG)G"
for t in $(seq 1 20); do docker info >/dev/null 2>&1 && break; sleep 3; done
if ! docker info >/dev/null 2>&1; then
  setsid dockerd >/tmp/dockerd_ui.log 2>&1 </dev/null &
  for t in $(seq 1 12); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
docker info >/dev/null 2>&1 || { echo "dockerd FAIL"; exit 1; }
echo "dockerd OK $(date +%H:%M:%S)"
# Only grade instances that actually carry a patch. A row with an empty patch is not a
# failed fix, it is an instance nobody worked, and scoring it as a failure is how a run
# that never happened turns into an accuracy number.
"$PY" - "$PREDS" "$RAW" "$OUT" <<'PYEOF'
import json,sys
preds=json.load(open(sys.argv[1]))
have={p['instance_id'] for p in preds if (p.get('patch') or '').strip()}
raw=[json.loads(l) for l in open(sys.argv[2])]
keep=[r for r in raw if r['instance_id'] in have]
open(sys.argv[3]+'/raw_graded.jsonl','w').write(''.join(json.dumps(r)+'\n' for r in keep))
open(sys.argv[3]+'/preds_graded.json','w').write(json.dumps([p for p in preds if p['instance_id'] in have]))
print('grading %d of %d instances (%d have no patch and are NOT scored)'
      % (len(keep),len(raw),len(raw)-len(keep)))
PYEOF
( while [ ! -f /tmp/ui_grade_done ]; do [ "$(freeG)" -lt 200 ] && docker system prune -af >/dev/null 2>&1; sleep 60; done ) & JAN=$!
( "$PY" -c "import json;[print('jefzda/sweap-images:'+json.loads(l)['dockerhub_tag']) for l in open('$OUT/raw_graded.jsonl')]" \
  | xargs -P3 -I{} bash -c 'for t in 1 2 3 4 5; do timeout 800 docker pull "{}" >/dev/null 2>&1 && exit 0; sleep 12; done' ) & PUL=$!
timeout 30000 "$PY" swe_bench_pro_eval.py --use_local_docker --num_workers 2 \
  --raw_sample_path "$OUT/raw_graded.jsonl" --patch_path "$OUT/preds_graded.json" \
  --scripts_dir run_scripts --dockerhub_username jefzda --output_dir "$OUT" 2>&1 | tail -3
touch /tmp/ui_grade_done; kill "$PUL" "$JAN" 2>/dev/null
"$PY" -c "
import json,sys
m=json.load(open('$OUT/eval_results.json'))
r=sum(1 for v in m.values() if v)
print('RESOLVED %d/%d graded = %.1f%%' % (r,len(m),100*r/max(1,len(m))))
print('end-to-end over the whole 40-instance slice: %d/40 = %.1f%%' % (r,100*r/40.0))
"
docker system prune -af >/dev/null 2>&1
echo "DONE_UI40 $(date +%H:%M:%S) free=$(freeG)G"
