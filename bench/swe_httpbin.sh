#!/bin/sh
# Local full httpbin for SWE-bench requests instances: serves BOTH http (:80) and https (:443,
# self-signed cert for httpbin.org) on the WSL host. The eval container maps httpbin.org ->
# 172.17.0.1 (docker bridge gateway) via /etc/hosts, so BOTH the HTTPBIN_URL suite AND tests that
# hardcode https://httpbin.org hit this fast/reliable server instead of the public httpbin.org
# (which 503s / is slow from here). Self-contained (writes its own server file -- no /mnt/c copy,
# which the Windows->wsl path layer mangles) and idempotent. Detached with setsid so it persists.
HBDIR=/opt/hb
mkdir -p "$HBDIR"; cd "$HBDIR" || exit 1

apk add --quiet openssl py3-pip 2>/dev/null || true
python3 -m pip install --quiet --break-system-packages httpbin flask "werkzeug<2.1" 2>/dev/null || true

cat > "$HBDIR/hb_server.py" <<'PYEOF'
import sys
from httpbin import app
mode = sys.argv[1] if len(sys.argv) > 1 else "http"
if mode == "https":
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/opt/hb/cert.pem", "/opt/hb/key.pem")
    app.run(host="0.0.0.0", port=443, ssl_context=ctx, threaded=True)
else:
    app.run(host="0.0.0.0", port=80, threaded=True)
PYEOF

if [ ! -f "$HBDIR/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -keyout "$HBDIR/key.pem" -out "$HBDIR/cert.pem" \
    -days 3650 -nodes -subj "/CN=httpbin.org" \
    -addext "subjectAltName=DNS:httpbin.org,DNS:localhost,IP:172.17.0.1,IP:127.0.0.1" >/dev/null 2>&1
fi

pkill -f hb_server 2>/dev/null || true
sleep 1
setsid python3 "$HBDIR/hb_server.py" http  </dev/null >"$HBDIR/h80.log"  2>&1 &
setsid python3 "$HBDIR/hb_server.py" https </dev/null >"$HBDIR/h443.log" 2>&1 &
echo "httpbin servers launched (http:80, https:443)"
