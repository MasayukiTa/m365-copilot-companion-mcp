"""Select a deterministic pilot subset of SWE-bench Lite and write the instance ids.
Run inside WSL with the swe-venv python. Keeps the HF cache on the Linux fs (fast).
"""
import json
import os

# point httpx/requests/ssl at the SYSTEM CA bundle (populated by `apk add ca-certificates`,
# and including any corporate intercept CA). huggingface_hub's httpx client otherwise uses
# certifi's bundle and fails TLS where urllib (system store) succeeds.
_SYS_CA = "/etc/ssl/certs/ca-certificates.crt"
if os.path.exists(_SYS_CA):
    os.environ.setdefault("SSL_CERT_FILE", _SYS_CA)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _SYS_CA)
    os.environ.setdefault("CURL_CA_BUNDLE", _SYS_CA)
os.environ.setdefault("HF_HOME", "/root/.cache/huggingface")
# corporate filter blocks HF's Xet CDN (cas-bridge.xethub.hf.co) -> force the legacy
# LFS/resolve path, which may be on an allowed host.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
from datasets import load_dataset

N = int(os.environ.get("SWE_N", "5"))
ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
rows = sorted(ds, key=lambda r: r["instance_id"])

# pilot: pick the first N by id (deterministic). Diversity/weighting comes later.
pick = rows[:N]
os.makedirs("/root/swe", exist_ok=True)
ids = [r["instance_id"] for r in pick]
with open("/root/swe/pilot_ids.txt", "w") as f:
    f.write("\n".join(ids) + "\n")

# also stash a compact per-task spec (for the agent harness later)
spec = [{
    "instance_id": r["instance_id"],
    "repo": r["repo"],
    "base_commit": r["base_commit"],
    "version": r.get("version"),
    "problem_statement": r["problem_statement"],
    "FAIL_TO_PASS": r["FAIL_TO_PASS"],
    "PASS_TO_PASS": r["PASS_TO_PASS"],
} for r in pick]
with open("/root/swe/pilot_spec.json", "w") as f:
    json.dump(spec, f, ensure_ascii=False)

print("N=%d selected:" % N)
for r in pick:
    print("  %s  (%s @ %s)" % (r["instance_id"], r["repo"], r["base_commit"][:10]))
