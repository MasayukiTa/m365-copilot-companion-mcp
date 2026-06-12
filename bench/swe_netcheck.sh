#!/bin/sh
echo "== curl HF root =="
curl -sS -m 20 -I https://huggingface.co 2>&1 | head -3
echo "== curl HF parquet (resolve) =="
curl -sS -m 25 -o /tmp/swe_test.parquet -w "http=%{http_code} size=%{size_download}\n" \
  "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/main/data/test-00000-of-00001.parquet" 2>&1 | tail -2
echo "== py urllib HF =="
/root/swe-venv/bin/python - <<'PY' 2>&1 | tail -3
import urllib.request as u
try:
    print("status", u.urlopen("https://huggingface.co", timeout=20).status)
except Exception as e:
    print("PYERR", type(e).__name__, str(e)[:160])
PY
echo "== certifi =="
/root/swe-venv/bin/python - <<'PY' 2>&1 | tail -2
try:
    import certifi; print("certifi", certifi.where())
except Exception as e:
    print("no certifi", e)
PY
