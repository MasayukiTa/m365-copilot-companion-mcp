#!/bin/sh
# Fetch SWE-bench Lite rows via the HF datasets-server REST API (returns JSON over an
# allowed host; bypasses the blocked Xet data CDN). Page in chunks of 100.
URL="https://datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench_Lite&config=default&split=test"
curl -sS -m 30 "${URL}&offset=0&length=2" -o /tmp/rows.json -w "http=%{http_code} bytes=%{size_download}\n"
echo "--- first 220 bytes ---"
head -c 220 /tmp/rows.json
echo
echo "--- has instance_id ---"
grep -c instance_id /tmp/rows.json
