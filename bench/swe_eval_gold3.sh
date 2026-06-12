#!/bin/sh
# 3-problem gold validation, low footprint (cache_level none + remove images after).
# The WSL vhdx lives on C: (256GB PC, ~18GB free), so keep peak tiny and clean up.
pgrep dockerd >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 10)
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
IDS=$(head -3 /root/swe/all_ids.txt | tr '\n' ' ')
echo "PILOT-3 IDS: $IDS"
cd /root/swe
/root/swe-venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name /root/swe/lite_local.json \
  --predictions_path gold \
  --max_workers 1 \
  --run_id pilot3_gold \
  --instance_ids $IDS \
  --cache_level none || echo "RC=$?"
echo "--- remove all swebench images + prune ---"
IIDS=$(docker images -q "swebench/*" 2>/dev/null)
[ -n "$IIDS" ] && docker rmi -f $IIDS 2>/dev/null
docker system prune -af 2>/dev/null | tail -1
echo "--- report ---"
cat /root/swe/*pilot3_gold*.json 2>/dev/null | head -c 800
echo
echo GOLD3_DONE
