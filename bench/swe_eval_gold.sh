#!/bin/sh
# Validate the SWE-bench eval pipeline end-to-end with GOLD patches on the 5 pilot tasks.
# If gold resolves 5/5, the whole chain works on this WSL2 host: Docker image build,
# github clone, dep install, test run, FAIL_TO_PASS/PASS_TO_PASS scoring.
set -e
pgrep dockerd >/dev/null 2>&1 || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 10)
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
IDS=$(head -5 /root/swe/all_ids.txt | tr '\n' ' ')
echo "PILOT IDS: $IDS"
cd /root/swe
/root/swe-venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name /root/swe/lite_local.json \
  --predictions_path gold \
  --max_workers 1 \
  --run_id pilot_gold \
  --instance_ids $IDS \
  --cache_level env || echo "RUN_EVAL_RC=$?"
echo "=== reports ==="
ls -1 /root/swe/*pilot_gold*.json 2>/dev/null || ls -1 *pilot_gold*.json 2>/dev/null || echo "(no report file found at cwd)"
echo GOLD_EVAL_DONE
